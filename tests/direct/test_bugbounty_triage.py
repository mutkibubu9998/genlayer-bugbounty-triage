"""Direct-mode tests for BugBountyTriage.

Fast, in-memory (~30ms). Leader function only - validator logic is NOT
exercised here (see tests/integration for full consensus). All web/LLM
calls are mocked.
"""

import json

REPO_URL = "https://example.com/repo/project-x"
EVIDENCE_URL = "https://example.com/evidence/poc-1"
TITLE = "Reentrancy in withdraw() drains the vault"
DESCRIPTION = (
    "Calling withdraw() twice in one transaction re-enters before the "
    "balance update, letting an attacker drain the vault."
)


def _hex(addr) -> str:
    """Raw 20-byte test address -> 0x-prefixed hex (contract returns checksummed)."""
    return "0x" + bytes(addr).hex()


def _register(contract, direct_vm, reporter, evidence_text="PoC steps included."):
    direct_vm.sender = reporter
    return contract.register_report(
        TITLE, DESCRIPTION, REPO_URL, EVIDENCE_URL, evidence_text
    )


def _mock_triage(direct_vm, severity, repo_page="Repo README and withdraw() source.",
                 evidence_page="PoC output: vault drained twice."):
    direct_vm.mock_web(
        r".*example\.com/repo/project-x.*",
        {"status": 200, "body": repo_page},
    )
    direct_vm.mock_web(
        r".*example\.com/evidence/poc-1.*",
        {"status": 200, "body": evidence_page},
    )
    direct_vm.mock_llm(
        r".*triaging a bug bounty report.*",
        json.dumps(
            {"severity": severity, "rationale": "Decisive fact from the evidence.",
             "confidence": 92}
        ),
    )


def test_register_report(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/bugbounty_triage.py")

    report_id = _register(contract, direct_vm, direct_alice)
    assert report_id == "rep_0"
    assert contract.get_report_count() == 1

    r = contract.get_report(report_id)
    assert r["reporter"].lower() == _hex(direct_alice).lower()
    assert r["title"] == TITLE
    assert r["description"] == DESCRIPTION
    assert r["repo_url"] == REPO_URL
    assert r["evidence_url"] == EVIDENCE_URL
    assert r["status"] == "OPEN"
    assert r["severity"] == ""
    assert r["rationale"] == ""
    assert r["triaged_ts"] == 0

    listed = contract.list_reports()
    assert len(listed["reports"]) == 1
    assert listed["reports"][0]["id"] == "rep_0"

    # A second reporter (bob) can register too - anyone may register
    report_id2 = _register(contract, direct_vm, direct_bob)
    assert report_id2 == "rep_1"
    assert contract.get_report_count() == 2
    assert contract.get_report("rep_1")["reporter"].lower() == _hex(direct_bob).lower()


def test_register_report_validation(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/bugbounty_triage.py")
    direct_vm.sender = direct_alice

    with direct_vm.expect_revert("[EXPECTED] Title is empty"):
        contract.register_report("   ", DESCRIPTION, REPO_URL, EVIDENCE_URL, "")
    with direct_vm.expect_revert("[EXPECTED] Description is empty"):
        contract.register_report(TITLE, "   ", REPO_URL, EVIDENCE_URL, "")
    with direct_vm.expect_revert("[EXPECTED] Repo URL is empty"):
        contract.register_report(TITLE, DESCRIPTION, "", EVIDENCE_URL, "")
    with direct_vm.expect_revert("[EXPECTED] Repo URL must be http(s)"):
        contract.register_report(TITLE, DESCRIPTION, "ftp://example.com/x",
                                 EVIDENCE_URL, "")
    with direct_vm.expect_revert("[EXPECTED] Evidence URL must be http(s)"):
        contract.register_report(TITLE, DESCRIPTION, REPO_URL,
                                 "ftp://example.com/x", "")
    with direct_vm.expect_revert("[EXPECTED] Title too long"):
        contract.register_report("x" * 201, DESCRIPTION, REPO_URL, EVIDENCE_URL, "")
    with direct_vm.expect_revert("[EXPECTED] Report not found: rep_99"):
        contract.get_report("rep_99")


def test_triage_critical(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/bugbounty_triage.py")
    report_id = _register(contract, direct_vm, direct_alice)

    # Cannot triage a report that does not exist
    with direct_vm.expect_revert("[EXPECTED] Report not found: rep_99"):
        contract.triage("rep_99")

    _mock_triage(direct_vm, "critical")
    severity = contract.triage(report_id)
    assert severity == "critical"
    r = contract.get_report(report_id)
    assert r["status"] == "TRIAGED"
    assert r["severity"] == "critical"
    assert r["rationale"] == "Decisive fact from the evidence."
    assert r["triaged_ts"] > 0

    # Triaging twice reverts
    with direct_vm.expect_revert("[EXPECTED] Report is not open for triage"):
        contract.triage(report_id)


def test_triage_invalid_and_low(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/bugbounty_triage.py")
    rep0 = _register(contract, direct_vm, direct_alice)
    rep1 = _register(contract, direct_vm, direct_bob)

    _mock_triage(direct_vm, "invalid",
                 evidence_page="Not a bug - expected behaviour per the docs.")
    assert contract.triage(rep0) == "invalid"
    assert contract.get_report(rep0)["severity"] == "invalid"

    direct_vm.clear_mocks()
    _mock_triage(direct_vm, "low",
                 evidence_page="Typo in an error message, no impact.")
    assert contract.triage(rep1) == "low"
    assert contract.get_report(rep1)["status"] == "TRIAGED"


def test_triage_text_only_evidence(direct_vm, direct_deploy, direct_alice):
    """Report with no evidence URL: classification comes from the text alone."""
    contract = direct_deploy("contracts/bugbounty_triage.py")
    direct_vm.sender = direct_alice
    report_id = contract.register_report(
        TITLE, DESCRIPTION, REPO_URL, "", "Full PoC text here."
    )

    direct_vm.mock_web(
        r".*example\.com/repo/project-x.*",
        {"status": 200, "body": "Repo README and withdraw() source."},
    )
    direct_vm.mock_llm(
        r".*triaging a bug bounty report.*",
        json.dumps({"severity": "high", "rationale": "Text proves the bug.",
                    "confidence": 88}),
    )

    assert contract.triage(report_id) == "high"
    assert contract.get_report(report_id)["status"] == "TRIAGED"


def test_close_report(direct_vm, direct_deploy, direct_alice, direct_bob, direct_owner):
    contract = direct_deploy("contracts/bugbounty_triage.py")
    report_id = _register(contract, direct_vm, direct_alice)

    # The reporter (alice) is NOT the contract owner here (deployer is the
    # default test account), so closing as alice must revert
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("[EXPECTED] Only the owner can close a report"):
        contract.close_report(report_id)

    # Owner closes an OPEN report
    direct_vm.sender = direct_owner
    contract.close_report(report_id)
    assert contract.get_report(report_id)["status"] == "CLOSED"

    # Cannot close twice
    with direct_vm.expect_revert("[EXPECTED] Report is already closed"):
        contract.close_report(report_id)

    # A closed report cannot be triaged
    with direct_vm.expect_revert("[EXPECTED] Report is not open for triage"):
        contract.triage(report_id)


def test_close_after_triage(direct_vm, direct_deploy, direct_alice, direct_owner):
    contract = direct_deploy("contracts/bugbounty_triage.py")
    report_id = _register(contract, direct_vm, direct_alice)
    _mock_triage(direct_vm, "medium")
    assert contract.triage(report_id) == "medium"

    direct_vm.sender = direct_owner
    contract.close_report(report_id)
    r = contract.get_report(report_id)
    assert r["status"] == "CLOSED"
    assert r["severity"] == "medium"  # triage result is kept after closing
