# bazaar-builds - Roadmap

Active work tracker. Stable repo context and operator notes live in `CLAUDE.md`; user-facing setup and usage live in `README.md`. Completed planning docs are removed rather than kept as historical checklists.

Status labels:
- `Open`: not yet complete.
- `Gate`: manual approval or evidence required before changing phase.
- `On Hold`: deferred until shadow output or curator need makes the work concrete.

## Automated Builds Pipeline - Gate

Current state: `pipeline_state.json` is `phase: shadow_cron` with `dry_run: true`. The daily GitHub Actions cron schedule exists (`0 6 * * *`). Scheduled shadow runs default to the `deterministic` (no-LLM) classifier and may fetch live sources, evaluate thresholds, write diff/proposal artifacts, upload workflow artifacts, and open/update a rolling `automated/stats-sync-<hero>` PR per hero against `main`. Each scheduled run force-pushes the per-hero branch; the PR is curator-merged or closed at discretion. Direct pushes to `main` are blocked by branch protection.

Shadow runs must not mutate `bazaar_coach` catalog files, open coach PRs, or promote the pipeline phase. `implementation` is the off switch, and `local_dry_run` is the rollback path for artifact-only runs without stats sidecar PRs.

Live cron remains disabled. Do not promote to `live_cron` until all of these are true:
- **Gate 1** — At least 2 healthy bazaardb patch windows have accumulated. `window_id` is keyed by patch label, so runs between patches reuse the same window; this gate is bound to The Bazaar's patch cadence and is the likely critical path regardless of how fast the classifier work lands.
- **Gate 2** — Deterministic/LLM classifier-produced output spans at least 14 calendar days, measured from the durable `classifier_started_at` (set once on the first real-classifier run). When no real classifier has run the full 14-calendar-day shadow span still applies; a waiver does **not** shorten this day count.
- **Gate 3** — `classifier_ready` is satisfied: a hero sidecar's `last_classifier_mode` is a real classifier (`llm` or `deterministic`), **or** the operator records an explicit waiver. A later `no_llm_shadow` run flips `last_classifier_mode` back and correctly re-blocks this gate.
- No malformed/unhealthy bazaardb shadow run in the last 14 days.
- The curator manually flips `phase`/`dry_run` after reviewing shadow artifacts and rollback behavior.

Gate 3 mechanism is implemented: the no-LLM `DeterministicClassifier` produces real carry/core/support tiers and `readiness.py` derives `classifier_ready` from sidecar `last_classifier_mode` (it is no longer hardcoded `False`).

Deterministic shadow artifacts now carry accepted semantics: `classification_mode: deterministic`, `semantic_classification: true`, `llm_provider: deterministic`, with candidate items in `candidate_core`/`candidate_support` rather than `candidate_pending`. Legacy `no_llm_shadow` artifacts (`classification_mode: no_llm_shadow`, `semantic_classification: false`, `llm_provider: none`, `classification_pending` labels) remain operational evidence only.

The promotion out of `local_dry_run` was validated from a Python 3.12.10 temporary environment. All five supported heroes completed controlled local dry runs with `--mock-llm`, live source fetches, temp-only artifacts, and exit code 0: Dooley, Karnok, Mak, Pygmalien, and Vanessa. Source health was healthy for bazaar-builds.net `2026-W19`, bazaardb `14.0 (Hotfix May 7)`, and Mobalytics meta builds `v541`; this is three healthy sources, not three temporal windows.

Rollback remains explicit: set `phase` back to `local_dry_run` with `dry_run: true` for artifact-only operation, or back to `implementation` to stop fetches, artifacts, stats writes, and PR actions.

## Semantic Classifier Strategy - Open

Before `live_cron` or any catalog-acceptance automation, decide whether to use the existing Claude-backed classifier, add a provider abstraction and alternate hosted provider, use another free/lower-cost provider, or proceed with an explicit operator waiver.

If evaluating a hosted provider, recheck current API availability, billing, quota, model names, data-use terms, and structured JSON reliability at implementation time. ChatGPT Plus/Pro subscriptions do not provide reusable OpenAI API billing for GitHub Actions.

## Review Tooling - On Hold

The pipeline currently emits diff JSON and proposal markdown artifacts, and `live_cron` can maintain one rolling PR per hero when enabled. Broader review tooling, such as a richer dashboard or more detailed per-proposal stats surface, remains deferred until shadow output shows a concrete curator pain point.
