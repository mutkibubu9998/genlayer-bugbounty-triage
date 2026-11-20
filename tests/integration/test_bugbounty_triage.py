"""Integration tests for BugBountyTriage against GLSim (localnet).

Full consensus: leader + validators run the triage with mocked web/LLM
responses (via transaction_context), so no real LLM API key is needed.

Requires a running simulator: glsim --port 4000 --validators 5
Run: gltest tests/integration/ -v -s --network localnet
"""

import json
import urllib.request

from gltest import create_accounts, get_contract_factory
from gltest.assertions import tx_execution_failed, tx_execution_succeeded

REPO_URL = "https://example.com/repo/project-x"
EVIDENCE_URL = "https://example.com/evidence/poc-1"
TITLE = "Reentrancy in withdraw() drains the vault"
DESCRIPTION = (
    "Calling withdraw() twice in one transaction re-enters before the "
    "balance update, letting an attacker drain the vault."
)
GLSIM_URL = "http://127.0.0.1:4000/api"


def _rpc(method, params):
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    req = urllib.request.Request(
        GLSIM_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    if "error" in payload:
        raise RuntimeError(f"GLSim RPC {method} failed: {payload['error']}")
    return payload["result"]


def _triage_context(severity):
    """Mocks for triage: repo page + evidence page + LLM severity."""
    return {
        "validators": [
            {
                "plugin_config": {
                    "mock_web_response": {
                        "nondet_web_request": {
                            REPO_URL: {
                                "status": 200,
                                "body": "Repo README and withdraw() source code.",
                            },
                            EVIDENCE_URL: {
                                "status": 200,
                                "body": "PoC output: vault drained twice in one tx.",
                            },
                        }
                    },
                    "mock_response": {
                        "response": {
                            ".*triaging a bug bounty report.*": json.dumps(
                                {
                                    "severity": severity,
                                    "rationale": "Decisive fact from the evidence.",
                                    "confidence": 92,
                                }
                            )
                        }
                    },
                }
            }
        ],
    }


def _deploy():
    factory = get_contract_factory("BugBountyTriage")
    contract = factory.deploy(args=[])
    alice, bob = create_accounts(2)
    return contract, alice, bob


def _register(contract, reporter):
    receipt = contract.connect(reporter).register_report(
        args=[TITLE, DESCRIPTION, REPO_URL, EVIDENCE_URL, "PoC steps included."]
    ).transact()
    assert tx_execution_succeeded(receipt)
    return "rep_0"


def test_full_flow_register_triage_critical():
    contract, alice, bob = _deploy()
    report_id = _register(contract, alice)

    report = contract.get_report(args=[report_id]).call()
    assert report["status"] == "OPEN"
    assert contract.get_report_count(args=[]).call() == 1

    # Triage with mocked repo/evidence pages + LLM severity (full consensus)
    receipt = contract.triage(args=[report_id]).transact(
        transaction_context=_triage_context("critical")
    )
    assert tx_execution_succeeded(receipt)
    report = contract.get_report(args=[report_id]).call()
    assert report["status"] == "TRIAGED"
    assert report["severity"] == "critical"
    assert report["rationale"] == "Decisive fact from the evidence."


def test_full_flow_triage_invalid():
    contract, alice, bob = _deploy()
    report_id = _register(contract, alice)

    receipt = contract.triage(args=[report_id]).transact(
        transaction_context=_triage_context("invalid")
    )
    assert tx_execution_succeeded(receipt)
    report = contract.get_report(args=[report_id]).call()
    assert report["status"] == "TRIAGED"
    assert report["severity"] == "invalid"


def test_close_report_owner_only_and_triage_closed_fails():
    contract, alice, bob = _deploy()
    report_id = _register(contract, alice)

    # Reporter (alice) is not the contract owner - close must fail
    receipt = contract.connect(alice).close_report(args=[report_id]).transact()
    assert tx_execution_failed(receipt)
    assert contract.get_report(args=[report_id]).call()["status"] == "OPEN"

    # Owner (default account) closes the report
    receipt = contract.close_report(args=[report_id]).transact()
    assert tx_execution_succeeded(receipt)
    assert contract.get_report(args=[report_id]).call()["status"] == "CLOSED"

    # A closed report cannot be triaged
    receipt = contract.triage(args=[report_id]).transact(
        transaction_context=_triage_context("high")
    )
    assert tx_execution_failed(receipt)


def test_triage_open_only():
    contract, alice, bob = _deploy()

    # Triaging a report that was never registered fails
    receipt = contract.triage(args=["rep_99"]).transact(
        transaction_context=_triage_context("high")
    )
    assert tx_execution_failed(receipt)
