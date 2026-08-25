# Governance

The control framework for running FinAgent-Nexus inside a regulated institution. This document
is written for a second line of defence — risk, compliance, model validation, internal audit —
rather than for engineers. For system structure see [`architecture.md`](architecture.md); for
the methodology and its rationale see [`SPEC.md`](../SPEC.md).

> **Scope of the claim.** This repository implements controls. It does not confer compliance.
> No configuration of this system substitutes for your own regulatory approval, Shari'ah board
> sign-off, or model validation process. What it provides is the evidence those processes ask
> for, generated as a by-product of running rather than retrofitted afterwards.

---

## 1. Separation of duties

Capability is assigned per agent and enforced **by construction, not by instruction**. An
instruction is a request; a capability the code never grants is a control.

| Agent | Market data | Sets weights | Renders verdict |
| --- | --- | --- | --- |
| MarketAnalyst | sole holder | no | no |
| WealthStrategist | no | sole holder | no |
| ComplianceOfficer | reference data only | no | no — reports findings |
| Orchestrator (LangGraph) | no | no | yes, via `aggregate_verdict` |

No agent holds two of the three powers:

- The analyst **cannot allocate** to its own favoured name.
- The strategist **cannot manufacture** the evidence supporting its own allocation.
- The reviewer **cannot approve** its own reasoning — and notably does not render the verdict at
  all. It returns findings; the verdict is computed in Python by `aggregate_verdict`.

That last row is the one to examine during validation. A reviewer that both critiques and
decides is a single point of failure wearing two hats.

---

## 2. The constitution as a controlled artefact

`src/finagent_nexus/constitution.py` is the rule set the ComplianceOfficer reviews against. It
is deliberately a **versioned file, not a prompt**: a Shari'ah board or compliance function
reviews it, signs it off, and every subsequent change appears in a diff with an author and a
date.

Each principle carries three governance-relevant fields:

| Field | Purpose |
| --- | --- |
| `severity` | `BLOCKING`, `MATERIAL`, or `ADVISORY` — drives the verdict deterministically |
| `check` | The id of an arithmetic check in `checks.py`, or empty when genuinely qualitative |
| `source` | The standard it derives from, so every finding is citable |

Shari'ah screening thresholds follow AAOIFI Shari'ah Standard No. 21 ratios in common use for
equity screening. **Institutions using a different board must edit `SHARIA_THRESHOLDS` and
re-run the eval harness before sign-off** — the Dow Jones Islamic Market and S&P Shariah
methodologies, for example, screen debt against a 36-month average market cap rather than total
assets, and will produce different outcomes on the same portfolio.

### Change management

| Change | Requires | Evidence produced |
| --- | --- | --- |
| Threshold adjustment | Shari'ah board or compliance sign-off | Diff of `SHARIA_THRESHOLDS` + eval re-run |
| New principle | Owner decision: arithmetic or judgement; routing owner | Diff + new golden cases |
| Severity reclassification | Risk committee — this changes what blocks issuance | Diff + `test_verdict.py` result |
| Risk appetite change | Risk committee | Diff of `aggregate_verdict`, one function, one test file |

Tuning risk appetite is deliberately a **reviewable code change rather than a prompt edit**. A
private bank may allow a material breach to pass with sign-off; a retail platform almost
certainly will not. Either posture is legitimate; both should be visible in version control.

---

## 3. The determinism boundary

Anything expressible as arithmetic is decided in Python — reproducible offline, forever, at zero
marginal cost, with no model involved. See
[`diagrams/determinism-boundary.mmd`](diagrams/determinism-boundary.mmd).

| Decided by arithmetic (`checks.py`) | Decided by judgement (model critique) |
| --- | --- |
| Debt / market cap, interest-bearing securities, impure revenue ratios | Suitability against stated tolerance and horizon |
| Prohibited sector membership; riba-bearing instrument kind | Excessive gharar in a proposed structure |
| Weights sum to 100%, long-only, single position limit | Fair, clear and not misleading presentation |
| Guarantee language / capital-at-risk disclosure | Evidential traceability of qualitative claims |

**The reviewer is never asked to re-judge arithmetic.** Deterministic results enter its prompt as
established facts marked "do not re-judge", its response is restricted to the qualitative
principles, and `_reconcile` filters the response back to exactly that set. Non-overridability
comes from never granting the opportunity.

For validators, this boundary is where to concentrate effort: everything on the left can be
tested exhaustively and offline, so validation attention belongs on the right.

**Every rule moved from right to left is a permanent reduction in model risk.**

---

## 4. Audit trail

One hash-chained JSONL file per run, named for its correlation id. Each event carries the
SHA-256 of the previous event, so altering or deleting event *n* invalidates every hash from *n*
onward.

```bash
finagent verify-audit audit/<correlation-id>.jsonl
```

Recorded per event: sequence, UTC timestamp, actor, action, status, structured detail (verdict,
findings by principle, blocking failures, remediations), model, token usage, latency, previous
hash, and hash.

Three properties worth stating plainly to an auditor:

1. **It is a hash chain, not a blockchain,** and does not pretend otherwise. It makes silent
   retroactive edits *detectable*, not impossible. For the full control, pair it with write-once
   storage — S3 Object Lock or a WORM volume.
2. **Tool calls are recorded by the client, not self-reported by the model.** A brief's evidence
   can therefore be reconciled against a transcript the model did not author.
3. **A halted run still closes its trail.** Failure is a state, not an exception — the process
   drains to `finalize` rather than dying mid-record. A partial trail is worse than none,
   because it is the artefact you have to explain.

---

## 5. Human escalation

`BLOCK` is not a failure mode. It is the system correctly declining to proceed.

It occurs when a blocking principle fails with no available remediation, or when the revision
budget is exhausted with defects outstanding. The state handed to the human carries the failing
principles, their evidence, and every remediation attempted — **a file, not an alert**.

Two behaviours are deliberate and should be confirmed during validation:

- **Fail-closed on unproven claims.** An `UNVERIFIABLE` finding on a blocking or material
  principle counts as a failure. An unproven claim is not an approved one.
- **A safety refusal is a governance event, never a blind retry.** `ModelRefusal` is recorded
  with its category and halts the run.

---

## 6. Evaluation as a control

`tests/eval/` holds golden cases with expected verdicts, run through the deterministic
`SyntheticMarketData` provider so results are reproducible on any machine, forever, with no
network and no API key.

Treat the eval suite as a **regression control on the rule set**, not merely as engineering
tests. The governance-relevant workflow is:

1. Propose a constitution or threshold change.
2. Re-run the suite.
3. Present the delta — which cases changed verdict — to the approving body.

A threshold change that moves no golden case is either inconsequential or under-tested; both are
worth knowing before sign-off.

---

## 7. Pre-production checklist

Before this system touches a client mandate:

- [ ] `SyntheticMarketData` replaced with a real `MarketDataProvider` implementation, and
      `FINAGENT_ALLOW_SYNTHETIC_DATA` left `false` so a missing provider fails loudly
      (enforced in `NexusRunner._resolve_provider`; regression tests in
      `tests/test_graph.py::TestSyntheticDataGuard`)
- [ ] Constitution reviewed and signed off by the Shari'ah board and/or compliance function
- [ ] `SHARIA_THRESHOLDS` reconciled to your board's methodology; eval harness re-run
- [ ] `aggregate_verdict` risk appetite confirmed by the risk committee
- [ ] Audit directory pointed at write-once storage; retention period set
- [ ] `verify-audit` scheduled as a periodic integrity control, not run only on demand
- [ ] Escalation path staffed — who receives a `BLOCK`, within what SLA
- [ ] Model routing reviewed: the ComplianceOfficer is **not** downgraded for cost reasons
- [ ] Baseline captured for the ROI metrics in SPEC §6, so improvement is measurable
- [ ] Client-facing disclosure reviewed — this system produces recommendations for human
      issuance, not automated advice

---

## 8. Configuration as a governance surface

Every knob that changes cost, latency, or governance posture lives in `config.py` and is
snapshotted into the audit trail, so "what model decided this, at what effort, under which
revision cap?" is answerable from a single object.

| Variable | Governance significance |
| --- | --- |
| `FINAGENT_MODEL` | Orchestration and compliance. **Downgrading the reviewer is a governance decision, not a cost decision.** |
| `FINAGENT_ANALYST_MODEL` | Analyst only — the legitimate place to route down for high-volume desks |
| `FINAGENT_EFFORT` | Reasoning depth; raise for complex mandates |
| `FINAGENT_MAX_REVISIONS` | Revision budget before escalation to a human |
| `FINAGENT_MAX_TOOL_ITERATIONS` | Tool-loop ceiling per agent turn |
| `FINAGENT_AUDIT_DIR` | Trail destination. **Empty keeps trails in memory only** — never in production |
| `FINAGENT_ALLOW_SYNTHETIC_DATA` | Permits fabricated prices. Defaults to `false`; **leave it false in production** — the guard is what turns a forgotten provider into a loud failure instead of a credible record of a fictional portfolio |

---

## 9. Known limits

Stated plainly, because a control framework that hides its gaps is not one:

- The bundled market data provider is **synthetic and deterministic**. It is a test fixture, not
  a market simulator, and must not inform live advice.
- The hash chain makes tampering detectable, not impossible. Write-once storage completes it.
- Portfolio construction is judgement-based, not mean-variance or risk-parity optimised.
  Optimisation under compliance constraints is a documented future extension.
- Risk metrics use a single-correlation approximation for portfolio volatility, isolated in
  `tools/risk_metrics.py` by design so it can be replaced with full covariance.
- The graph is strictly sequential, which is what permits a single hash chain. Parallel fan-out
  requires per-branch sub-chains and an explicit reducer.
- Qualitative principles depend on model judgement and carry irreducible model risk. That is the
  reason for the determinism boundary, the eval harness, and human escalation — not a residual
  the design ignores.
