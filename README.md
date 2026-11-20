# BugBounty Triage — Bug Severity Classification by GenLayer Consensus

A standalone GenLayer Intelligent Contract that triages bug bounty reports:
anyone registers a bug report (title, description, affected repo URL,
evidence URL/text), and anyone can trigger triage. The leader fetches the
repo and evidence pages and an LLM classifies the severity as `critical` /
`high` / `medium` / `low` / `invalid`, returning strict JSON with a short
rationale. Validators independently re-fetch the same evidence and re-run
the same classification; the severity is accepted only on an exact
severity-field match, so no single leader can set a severity on its own
say-so.

**Deployed on StudioNet (gasless):** `0xDB7768aFbB023E6E5a5a7F3FB5AB66C9Fff0696B` — see
[explorer](https://explorer-studio.genlayer.com/address/0xDB7768aFbB023E6E5a5a7F3FB5AB66C9Fff0696B)

Deploy transaction: `0x1bb67a2393e049c7521256fa20a595d7442ddf6d0a9299e2f64f141816dd550d` (ACCEPTED, 3/5 validators agreed, 2 idle).

## How it works

1. **Anyone registers a bug report** (`register_report`): title, description,
   affected repo URL, and evidence (URL and/or text).
2. **Anyone triggers triage** (`triage`). The leader fetches the repo page
   and the evidence page and an LLM classifies the severity:
   `critical` (remotely exploitable, severe impact), `high` (serious impact,
   harder exploit path), `medium` (real bug, moderate impact or workaround),
   `low` (minor bug, negligible impact), or `invalid` (not a real bug).
3. **Validators independently re-fetch the same evidence and re-run the
   same classification.** The custom comparative validator requires exact
   agreement on the `severity` field — no single leader can decide alone.
4. **The contract owner can close/withdraw a report** (`close_report`),
   from OPEN or TRIAGED state.

No money moves in this contract at all: no GEN transfers, no payments, no
payable methods. It is a pure classification primitive — the same contract
serves any repo's bug triage because the report content and the evidence are
per-report inputs, not hard-coded prompts.

## State machine

```
OPEN ──triage (consensus severity)──▶ TRIAGED
  │                                     │
  └──────────close_report───────────────┘──▶ CLOSED
```

## Why this is a reusable primitive, not a thin LLM wrapper

- The report, the classification, and the state transition are all on-chain
  and gated by independent validator re-verification of the evidence — the
  LLM never decides alone.
- Equivalence is strict on the decision field (`severity` must match
  exactly); free-text rationales may differ.
- Error classification follows the GenLayer convention
  (`[EXPECTED]` / `[EXTERNAL]` / `[TRANSIENT]` / `[LLM_ERROR]`) so
  validators compare failure paths correctly.
- The contract is generic: one deployment serves any project's bug triage.

## Contract methods

| Method | Type | Description |
|---|---|---|
| `register_report(title, description, repo_url, evidence_url, evidence_text)` | write | Register a bug report (anyone), returns `rep_N` |
| `triage(report_id)` | write | Consensus severity classification, returns severity |
| `close_report(report_id)` | write | Owner closes/withdraws a report |
| `get_report(report_id)` | view | Full report record |
| `list_reports()` | view | All reports |
| `get_report_count()` | view | Number of reports |

## Tests

```bash
# Lint (genvm-lint 0.11.0)
genvm-lint check contracts/bugbounty_triage.py

# Direct mode (fast, in-memory; leader only, mocked web/LLM)
pytest tests/direct/ -v

# Integration (GLSim, full leader + validator consensus)
glsim --port 4000 --validators 5
gltest tests/integration/ -v -s --network localnet
```

- Direct: 7 tests — registration + validation, triage critical/invalid/low,
  text-only evidence, close (owner-only), close after triage.
- Integration: 4 tests — full register → triage critical flow, triage
  invalid flow, close owner-only + triage of a closed report fails,
  triage of an unregistered report fails.

## Deploy (StudioNet, gasless)

```bash
genlayer network set studionet
genlayer deploy --contract contracts/bugbounty_triage.py
```

> Note: `genlayer deploy` (CLI 0.39.2) hangs after printing the deployment
> parameters when run under a PTY on this machine (spinner loop, no tx
> submitted). The same deploy works in seconds via
> [`deploy_direct.cjs`](deploy_direct.cjs), which calls `genlayer-js`
> directly with the `genlayer-builder` keystore (password read from
> `~/workspace/genlayer/.builder-account-password` at runtime, never
> stored in the repo):
>
> ```bash
> node deploy_direct.cjs
> ```

## License

MIT — see [LICENSE](LICENSE).
