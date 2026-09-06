# FinAgent-Nexus — Multi-Agent Financial Intelligence

**Agentic AI adoption for financial services.** Closing the gap between AI potential and financial reality.

[![CI](https://github.com/asadullah48/finagent-nexus/actions/workflows/ci.yml/badge.svg)](https://github.com/asadullah48/finagent-nexus/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20|%203.12%20|%203.13-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[العربية](README.ar.md) · [Technical specification](SPEC.md) · [Architecture](docs/architecture.md) · [Governance](docs/governance.md)

---

## The gap this closes

Most financial institutions have run an AI pilot. Far fewer have put one into production. The pilot is rarely what fails — what fails is the distance between a demo that produces a plausible answer and a system a second line of defence will sign off on.

That distance is made of specific, boring things:

| The pilot has | Production requires |
| --- | --- |
| An answer | An answer, its evidence, and the record of how it was reached |
| A prompt asking the model to be compliant | Rules that hold whatever the model outputs |
| A conversation between agents | A control flow a compliance officer can certify |
| "It worked in the demo" | A regression suite that fails the build when it stops working |
| A cost estimate | Per-decision cost and latency, measured |

FinAgent-Nexus is a reference implementation of the second column.

---

## What it is

Three specialised agents cooperating inside an explicit **Plan-Act-Verify** state machine:

- **MarketAnalyst** — the only agent with tool access. Retrieves market data, computes risk metrics, and produces an evidence-backed brief. Every figure it reports traces to a recorded tool call.
- **ComplianceOfficer** — applies a written constitution of Shari'ah and regulatory principles. Deterministic screens run as code; qualitative principles are reviewed by the model. It reports findings; it does not decide outcomes.
- **WealthStrategist** — turns the brief into a portfolio, then rebuilds it against compliance remediations until it clears or the revision budget runs out.

```
START ─► plan ─► act ─► synthesize ─► verify ─┬─► finalize ─► END
                 ▲                            │
                 └────────── revise ──────────┘
```

There is deliberately **no edge from `synthesize` to `finalize`**. Nothing reaches a client without passing verification. That is a property of the graph, not of a prompt.

---

## Five design decisions worth defending in a risk committee

**1. Compliance arithmetic is code, not prompting.**
The AAOIFI screens — 30% debt to market cap, 30% interest-bearing securities, 5% impure revenue — are evaluated in `src/finagent_nexus/checks.py`. A model cannot reason its way past a 31% ratio, and an auditor can reproduce every screening finding offline with no API key.

**2. The model reports findings; Python renders the verdict.**
`aggregate_verdict()` maps per-principle findings to PASS / REVISE / BLOCK. Your risk policy is therefore one reviewable function with its own test file, not a paragraph of prompt text that changes meaning when the model does.

**3. The system fails closed.**
An `unverifiable` finding on a blocking principle counts as a failure. A recommendation that cannot be proven compliant is not approved.

**4. Evidence cannot be fabricated.**
The analyst proposes an evidence list; the framework discards it and substitutes the tool receipts that actually executed. An agent cannot cite a call it did not make.

**5. The audit trail is tamper-evident.**
Every event carries the SHA-256 of its predecessor. Altering event *n* invalidates every hash after it, and `finagent verify-audit` detects it — from the file alone, without credentials or trust in this codebase.

---

## Quick start

```bash
git clone <your-fork> && cd finagent-nexus
uv venv --python 3.11 && uv pip install -e ".[dev]"
cp .env.example .env          # add ANTHROPIC_API_KEY, or run `ant auth login`

pytest                        # offline suite — no API key needed
finagent run --mandate examples/mandates/balanced_sharia.json \
  --allow-synthetic-data   # demo only: prices are fabricated, see the note below
```

**Only `finagent run` needs a model.** The other three commands are the deterministic half, and
they run on `pip install -r requirements.txt` alone — no API key, no network, and with neither
`anthropic` nor `langgraph` installed:

```bash
finagent verify-audit audit/<id>.jsonl   # recompute the hash chain
finagent replay       audit/<id>.jsonl   # reconstruct the decision from the trail
finagent eval --dataset tests/eval/datasets/golden_cases.json --allow-synthetic-data
```

`replay` refuses a trail whose chain does not verify. A tidy report rendered from an altered record
would launder exactly the tampering the chain exists to expose. `eval` exits non-zero when a screen
stops catching what it claims to, so scheduled re-validation is a build step rather than a document
somebody has to read.

Three worked demonstrations:

```bash
python examples/market_analysis.py     # tool-driven analysis, evidence receipts
python examples/compliance_check.py    # the constitution catching real breaches, offline
python examples/wealth_strategy.py     # the full Plan-Act-Verify loop
```

`compliance_check.py` runs with **no API key at all** — the deterministic screens are pure functions. That is the point.

> **On `--allow-synthetic-data`.** The bundled market data provider fabricates deterministic prices from a hash of the symbol. It exists so the repository is runnable and the eval harness is reproducible — it is **not** a market simulator. Synthetic data is therefore opt-in and never inherited from a default: a runner constructed without a real `MarketDataProvider` raises rather than quietly falling back. Forgetting to inject a provider would otherwise be indistinguishable from a working deployment, since every other control still passes and the audit trail would faithfully notarise a recommendation built on invented data.

---

## What you get out of a run

```
Outcome        : PASS
Revisions used : 1 / 2

Symbol           Weight   Rationale
SUKUK.GCC         30.0%   Sukuk anchor sized to the conservative tolerance…
2222.SR           22.0%   Energy exposure; 12.4% debt/market cap clears AAOIFI…
…
TOTAL            100.0%

Compliance findings
[PASS] SHARIA-SCREEN-02: Debt to market cap is within the AAOIFI threshold.
        evidence: Highest observed = 26.5% (threshold 30%).
[PASS] REG-CONC-02: All positions are within the single-name concentration limit.
…

Audit
14 events | chain valid: True | tokens in/out: 48,210/9,884 (cached 31,004) | 38,412 ms
Trail written to audit/9f2c….jsonl
```

Every line of that report is reconstructable from the audit trail six months later — run
`finagent replay` on the trail file and you get it back, including the summary, each holding's
rationale, the disclosures, and every finding's reasoning.

That is a deliberate and recent correction. The chain always made the record tamper-evident, but
the record itself held only weights and statuses: it could prove nobody had edited it, and could
not tell you what the client was actually told. A hash chain over an incomplete record proves,
very rigorously, that an incomplete record has not been altered. What gets notarised at issuance
is now its own reviewable policy in `retention.py`.

---

## Where the commercial return comes from

The claim is not "AI makes advisers faster." The claim is that four specific costs move, and that this architecture makes each of them measurable rather than asserted. Baselines are yours to supply — the harness in `tests/eval/` produces the "after" column.

| Lever | What changes | How it is measured here |
| --- | --- | --- |
| **Compliance review cycle** | Deterministic screens catch breaches before a human sees the file, so reviewers spend their time on judgement cases | `tests/eval/` reports catch rate per principle; `revisions_used` per run |
| **Time to production** | The governance artefacts — constitution, audit schema, eval suite — exist on day one instead of being retrofitted during model risk review | Days from mandate to sign-off, tracked against your own SR 11-7 / internal validation process |
| **Cost per decision** | Prompt caching on the constitution prefix, model routing per agent, bounded revision loops | `cache_read_input_tokens` and token totals in every audit summary |
| **Rework** | A breach caught by an automated screen costs a revision loop; the same breach caught after issuance costs a remediation programme | Blocking failures per 100 runs, from the audit trails |

Two honest caveats. The bundled market data is synthetic — it exists so the repo is runnable and the eval suite is reproducible, and it must be replaced with your golden source before any live use. And the table above is a measurement design, not a benchmark: the numbers are yours, produced by your baselines through this harness.

---

## Segments

| Segment | Where this architecture lands first |
| --- | --- |
| **Wealth & private banking** | Mandate-aware portfolio construction with Shari'ah and suitability screening built into the control flow |
| **Retail & corporate banking** | Product recommendation and credit narrative generation under fair-treatment and disclosure rules |
| **SME banking** | Scaled advisory where a human review of every file is uneconomic but an unreviewed file is unacceptable |
| **Insurance** | Takaful-compliant product structuring; underwriting narratives with evidence traceability |

The constitution is the part you localise. Everything else — the graph, the audit chain, the eval harness — is domain-independent.

---

## Adopting it

**Path one — extend this repository.** Replace `SyntheticMarketData` with your data provider, edit `constitution.py` with your Shari'ah board's thresholds and your regulator's rules, add golden cases to `tests/eval/datasets/`, and run the suite in CI.

**Path two — take the patterns.** The five decisions above transfer to any agentic system in a regulated domain. The specific rules are financial; the architecture is not.

Either way the operating principle is the same: **production in mind from day one**. Governance retrofitted after a pilot is the single most common reason financial-services AI never ships.

---

## Repository layout

```
src/finagent_nexus/
  agents/            MarketAnalyst, ComplianceOfficer, WealthStrategist
  tools/             Market data provider (Protocol) + risk metrics + tool specs
  constitution.py    The written rules — the artefact your board signs off
  checks.py          Deterministic screens; pure functions, no model
  verdict.py         Risk appetite: findings -> PASS / REVISE / BLOCK
  provider_policy.py Whether a run may use fabricated prices
  retention.py       What gets notarised at issuance
  evaluation.py      Golden-case replay through the screens
  graph.py           Plan-Act-Verify state machine
  audit.py           Hash-chained, tamper-evident event trail
  llm.py             Typed Claude access: structured output, bounded tool loops
  cli.py             run · verify-audit · replay · eval
api/screen.py        The deployed screening endpoint — deterministic half only
examples/            Three runnable demonstrations
tests/               Unit tests + eval harness with golden cases
docs/                Architecture, governance, diagrams
.github/workflows/   CI: lint, types, tests, boundary, tamper, golden cases
SPEC.md              Full technical specification
```

The five modules above `graph.py` are the deterministic half. None of them imports a model client,
and CI proves it by installing the project with `anthropic` and `langgraph` genuinely absent and
running the screens anyway.

---

## Status and limits

This is a reference implementation, not a licensed advisory product. It is not investment advice, it does not constitute a Shari'ah opinion, and the screening thresholds must be confirmed by your own Shari'ah board — methodologies differ materially between AAOIFI, Dow Jones Islamic Market, and S&P Shariah, and choosing between them is a governance decision, not a technical one.

Licensed under the [MIT License](LICENSE).

Author
Built by Asadullah Shafique.

🔗 Explore my portfolio showcasing Agentic AI projects and real-world applications: asadullahshafique-devunity.vercel.app


