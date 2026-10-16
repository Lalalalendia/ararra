# Fidelity/cost pilot checkpoint — Chaptera 0.5.0

**Verdict: NO-GO for making `atoms_v1` the default.** The live Gemini pilot and human literary comparison have **not** been run. The measurements below use only locally generated synthetic text and a deterministic fake provider. They demonstrate **structural protocol behavior** and character overhead, not model quality or monetary cost.

## Reproducible offline run

```bash
pip install -e '.[test]'
pytest -q
chaptera-eval --output offline-report.json
```

Three deliberately small synthetic fixtures: (1) two Chinese paragraphs, (2) mixed Japanese/Chinese with nested emphasis, and (3) repeated CJK fragments. All contain invented test text rather than third-party chapters. The comparison runs both protocols through the real translation core and a deterministic simulated provider.

| Fixture | `html_v0` prompt characters | `atoms_v1` prompt characters | Relative `atoms_v1` overhead | Provider requests each | Structural QA |
|---|---:|---:|---:|---:|---|
| 1 — Chinese paragraphs | 804 | 1,066 | +32.6% | 1 | both passed |
| 2 — mixed/nested | 863 | 1,115 | +29.2% | 1 | both passed |
| 3 — repeated CJK | 1,260 | 1,516 | +20.3% | 1 | both passed |
| **Total (synthetic only)** | **2,927** | **3,697** | **+26.3%** | **3 each** | **6/6 passed** |

The `atoms_v1` result includes per-source-atom provenance; `html_v0` does not. These fixtures are too small to represent long chapters, real provider retries or repair frequency. Longer contexts can multiply prompt overhead because the same context appears in multiple atom batches.

### What is NOT measured

- **Actual tokens:** unavailable (`usageMetadata` is absent with simulated responses). Character counts must not be described as actual tokens.
- **Price:** no price claim or USD estimate. Provider pricing and input/output/thought/cached token categories may differ by model and date.
- **Translation quality:** no human bilingual review, no source-faithfulness verdict, no factual or literary equivalence claim.
- **Production robustness:** no live 429/rate-limit or long-context truncation distribution.

## Explicit opt-in live experiment

Only run against chapters the team is authorized to submit to the API, with an appropriately budgeted account:

```bash
export CHAPTERA_GEMINI_API_KEY='your-key-from-secure-storage'
export CHAPTERA_EVAL_ALLOW_PAID='YES'
chaptera-eval --live --authorize-billable --chapter-file /secure/path/chapter.html \
  --model gemini-3.8-flash --max-requests 6 --max-prompt-chars 30000 \
  --output /secure/path/evaluation-metrics.json
```

**Nothing live is executed by CI or the offline command.** Live requests require *all* four gates: `--live`, `--authorize-billable`, `CHAPTERA_EVAL_ALLOW_PAID=YES` and `CHAPTERA_GEMINI_API_KEY`, plus an explicitly supplied chapter file. Both protocols share one max-request budget; prompt-length checks happen **before** sending requests. Do not commit the chapter, keys or the JSON report to this public repository.

The report contains only aggregate sizes, transport names, QA status/stage, provider request counts, atom proof counts, and token counters when present — **never prompt, chapter text, response text, API key or exception body**. The real-model run can still incur charges and return semantically incorrect text despite passing structural checks.

## Criteria before switching defaults

1. Obtain authorized live samples of varying length, scripts, markup and terminology, with current provider token counts and documented spend.
2. Compare both protocols on coverage, targeted repairs, truncation, retries, structural correctness, and *human-reviewed* adequacy, continuity and literary quality.
3. Establish explicit acceptance thresholds against the current baseline and a rollback plan for new-job defaults.
4. Keep `html_v0` handling for previously persisted jobs irrespective of any future default change.

**The default remains `html_v0`.** Issue #6 should remain open for the authorized live/human comparison.
