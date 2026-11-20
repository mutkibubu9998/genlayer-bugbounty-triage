# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""BugBounty Triage - bug severity classification by GenLayer consensus.

A project owner (or any reporter) registers a bug report: title, description,
affected repo URL, and evidence (URL and/or text). Anyone can then call
triage: the leader fetches the repo/evidence pages and an LLM classifies the
severity as one of critical / high / medium / low / invalid, returning strict
JSON with a short rationale. Every validator independently re-fetches the
same evidence and re-runs the same classification; the severity is accepted
ONLY on an exact severity-field match (comparative validation), so no single
leader can set a severity on its own say-so.

State machine:
    OPEN -> TRIAGED        (consensus severity + rationale stored)
    OPEN -> CLOSED         (contract owner closes/withdraws the report)
    TRIAGED -> CLOSED      (contract owner closes after triage)

No money moves in this contract at all: no GEN transfers, no payments, no
payable methods. It is a pure classification primitive - the same contract
serves any repo's bug triage because the report content and the evidence are
per-report inputs, not hard-coded prompts.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *

# ---------------------------------------------------------------------------
# Error classification (validators compare these prefixes, see _handle_leader_error)
# ---------------------------------------------------------------------------
ERROR_EXPECTED = "[EXPECTED]"  # Business logic (deterministic) - exact match required
ERROR_EXTERNAL = "[EXTERNAL]"  # External API 4xx (deterministic) - exact match required
ERROR_TRANSIENT = "[TRANSIENT]"  # Network/5xx (non-deterministic) - agree if both transient
ERROR_LLM = "[LLM_ERROR]"  # LLM misbehavior - always disagree, force rotation

MAX_TITLE_LEN = 200
MAX_DESCRIPTION_LEN = 2000
MAX_URL_LEN = 500
MAX_EVIDENCE_LEN = 2000
EVIDENCE_CHARS = 6000
SEVERITIES = ("critical", "high", "medium", "low", "invalid")


@allow_storage
@dataclass
class Report:
    id: str
    reporter: Address
    title: str
    description: str
    repo_url: str
    evidence_url: str
    evidence_text: str
    status: str  # OPEN | TRIAGED | CLOSED
    severity: str  # "" | critical | high | medium | low | invalid
    rationale: str
    created_ts: u256
    triaged_ts: u256


class BugBountyTriage(gl.Contract):
    owner: Address
    report_count: u256
    reports: TreeMap[str, Report]
    report_ids: DynArray[str]

    def __init__(self):
        self.owner = gl.message.sender_address
        self.report_count = u256(0)

    # ------------------------------------------------------------------
    # Helpers (deterministic)
    # ------------------------------------------------------------------
    def _now(self) -> u256:
        return u256(int(datetime.now(timezone.utc).timestamp()))

    def _get_report(self, report_id: str) -> Report:
        if report_id not in self.reports:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Report not found: {report_id}")
        return self.reports[report_id]

    def _report_to_dict(self, r: Report) -> dict:
        return {
            "id": r.id,
            "reporter": r.reporter.as_hex,
            "title": r.title,
            "description": r.description,
            "repo_url": r.repo_url,
            "evidence_url": r.evidence_url,
            "evidence_text": r.evidence_text,
            "status": r.status,
            "severity": r.severity,
            "rationale": r.rationale,
            "created_ts": int(r.created_ts),
            "triaged_ts": int(r.triaged_ts),
        }

    def _fetch_page(self, url: str) -> str:
        """Fetch one URL, classifying failures for consensus."""
        res = gl.nondet.web.get(url)
        if res.status == 200:
            pass
        elif 400 <= res.status < 500:
            raise gl.vm.UserError(f"{ERROR_EXTERNAL} URL returned {res.status}: {url}")
        else:
            raise gl.vm.UserError(f"{ERROR_TRANSIENT} URL returned {res.status}: {url}")
        try:
            return res.body.decode("utf-8", errors="ignore")[:EVIDENCE_CHARS]
        except Exception:
            raise gl.vm.UserError(f"{ERROR_TRANSIENT} Page undecodable: {url}")

    def _parse_severity(self, raw) -> dict:
        """Defensively parse the LLM severity JSON (dict or raw string)."""
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str):
            text = raw
            first = text.find("{")
            last = text.rfind("}")
            if first < 0 or last < 0:
                raise gl.vm.UserError(f"{ERROR_LLM} LLM returned no JSON object")
            text = text[first : last + 1]
            text = re.sub(r",(?!\s*?[\{\[\"\'\w])", "", text)
            try:
                data = json.loads(text)
            except Exception:
                raise gl.vm.UserError(f"{ERROR_LLM} LLM JSON unparsable")
        else:
            raise gl.vm.UserError(f"{ERROR_LLM} LLM returned non-dict: {type(raw)}")

        if not isinstance(data, dict):
            raise gl.vm.UserError(f"{ERROR_LLM} LLM JSON is not an object")

        severity = data.get("severity")
        if severity is None:
            for alt in ("level", "classification", "rating"):
                if alt in data:
                    severity = data[alt]
                    break
        if severity is None:
            raise gl.vm.UserError(
                f"{ERROR_LLM} Missing 'severity'. Keys: {list(data.keys())}"
            )

        severity = str(severity).strip().lower()
        if severity not in SEVERITIES:
            raise gl.vm.UserError(f"{ERROR_LLM} Invalid severity: {severity}")

        rationale = data.get("rationale", "")
        if not isinstance(rationale, str):
            rationale = str(rationale)
        return {"severity": severity, "rationale": rationale[:500]}

    def _handle_leader_error(self, leaders_res, leader_fn) -> bool:
        """Canonical error comparison for validators (see write-contract skill)."""
        leader_msg = leaders_res.message if hasattr(leaders_res, "message") else ""
        try:
            leader_fn()
            return False  # Leader errored, validator succeeded - disagree
        except gl.vm.UserError as e:
            validator_msg = e.message if hasattr(e, "message") else str(e)
            if validator_msg.startswith(ERROR_EXPECTED) or validator_msg.startswith(
                ERROR_EXTERNAL
            ):
                return validator_msg == leader_msg
            if validator_msg.startswith(ERROR_TRANSIENT) and leader_msg.startswith(
                ERROR_TRANSIENT
            ):
                return True
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Register a bug report (anyone may register)
    # ------------------------------------------------------------------
    @gl.public.write
    def register_report(
        self,
        title: str,
        description: str,
        repo_url: str,
        evidence_url: str,
        evidence_text: str,
    ) -> str:
        title = title.strip()
        if len(title) == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Title is empty")
        if len(title) > MAX_TITLE_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Title too long")

        description = description.strip()
        if len(description) == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Description is empty")
        if len(description) > MAX_DESCRIPTION_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Description too long")

        repo_url = repo_url.strip()
        if len(repo_url) == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Repo URL is empty")
        if len(repo_url) > MAX_URL_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Repo URL too long")
        if not (repo_url.startswith("http://") or repo_url.startswith("https://")):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Repo URL must be http(s)")

        evidence_url = evidence_url.strip()
        evidence_text = evidence_text.strip()
        if len(evidence_url) > MAX_URL_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Evidence URL too long")
        if len(evidence_url) > 0 and not (
            evidence_url.startswith("http://") or evidence_url.startswith("https://")
        ):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Evidence URL must be http(s)")
        if len(evidence_text) > MAX_EVIDENCE_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Evidence text too long")

        report_id = f"rep_{int(self.report_count)}"
        report = Report(
            id=report_id,
            reporter=gl.message.sender_address,
            title=title,
            description=description,
            repo_url=repo_url,
            evidence_url=evidence_url,
            evidence_text=evidence_text,
            status="OPEN",
            severity="",
            rationale="",
            created_ts=self._now(),
            triaged_ts=u256(0),
        )
        self.reports[report_id] = report
        self.report_ids.append(report_id)
        self.report_count = self.report_count + u256(1)
        return report_id

    # ------------------------------------------------------------------
    # Triage a report by consensus severity classification
    # ------------------------------------------------------------------
    @gl.public.write
    def triage(self, report_id: str) -> str:
        report = self._get_report(report_id)
        if report.status != "OPEN":
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Report is not open for triage: {report.status}"
            )

        title = report.title
        description = report.description
        repo_url = report.repo_url
        url = report.evidence_url
        text = report.evidence_text

        def leader_fn() -> dict:
            repo_page = self._fetch_page(repo_url)
            evidence_page = ""
            if len(url) > 0:
                evidence_page = self._fetch_page(url)

            prompt = f"""You are triaging a bug bounty report for a software project.
Bug title: {title}
Bug description: {description}
Affected repo: {repo_url}
Repo page fetched:
{repo_page}
Evidence submitted by the reporter (text): {text}
Evidence page fetched from {url if len(url) > 0 else "(no evidence URL provided)"}:
{evidence_page}

Classify the severity of this bug using ONLY the evidence above. Do not
assume facts that are not in the evidence.
Respond with JSON only, nothing else:
{{"severity": "critical" | "high" | "medium" | "low" | "invalid", "rationale": "<one sentence quoting the decisive fact>", "confidence": <0-100>}}
Use "critical" only for a remotely exploitable issue with severe impact
(e.g. loss of funds, unauthenticated remote code execution). Use "high" for
a serious issue with real impact but a harder exploit path or limited scope.
Use "medium" for a real bug with moderate impact or a workaround. Use "low"
for a minor bug with negligible impact. Use "invalid" if the report is not
a real bug (duplicate, user error, expected behaviour, or unproven)."""
            raw = gl.nondet.exec_prompt(prompt, response_format="json")
            return self._parse_severity(raw)

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return self._handle_leader_error(leaders_res, leader_fn)
            try:
                mine = leader_fn()
            except gl.vm.UserError:
                return False
            except Exception:
                return False
            leader_data = leaders_res.calldata
            if not isinstance(leader_data, dict):
                return False
            # Exact agreement on the decision field; free-text rationale may differ.
            return leader_data.get("severity") == mine.get("severity")

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
        severity = result["severity"]
        rationale = result.get("rationale", "")

        report.severity = severity
        report.rationale = rationale
        report.status = "TRIAGED"
        report.triaged_ts = self._now()
        self.reports[report_id] = report
        return severity

    # ------------------------------------------------------------------
    # Contract owner closes/withdraws a report (OPEN or TRIAGED)
    # ------------------------------------------------------------------
    @gl.public.write
    def close_report(self, report_id: str) -> None:
        report = self._get_report(report_id)
        if gl.message.sender_address != self.owner:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Only the owner can close a report")
        if report.status == "CLOSED":
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Report is already closed")

        report.status = "CLOSED"
        self.reports[report_id] = report

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------
    @gl.public.view
    def get_report(self, report_id: str) -> dict:
        return self._report_to_dict(self._get_report(report_id))

    @gl.public.view
    def list_reports(self) -> dict:
        return {
            "reports": [self._report_to_dict(self.reports[rid]) for rid in self.report_ids]
        }

    @gl.public.view
    def get_report_count(self) -> u256:
        return self.report_count
