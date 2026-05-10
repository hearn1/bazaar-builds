# bazaar-builds - Roadmap

Active work tracker. Stable repo context and operator notes live in `CLAUDE.md`; user-facing setup and usage live in `README.md`. Completed planning docs are removed rather than kept as historical checklists.

Status labels:
- `Open`: not yet complete.
- `Gate`: manual approval or evidence required before changing phase.
- `On Hold`: deferred until shadow output or curator need makes the work concrete.

## Automated Builds Pipeline - Gate

Current state: `pipeline_state.json` is `phase: shadow_cron` with `dry_run: true`. The daily GitHub Actions cron schedule exists (`0 6 * * *`). Scheduled shadow runs default to deterministic `no_llm_shadow` and may fetch live sources, evaluate thresholds, write diff/proposal artifacts, upload workflow artifacts, and open/update a rolling `automated/stats-sync-<hero>` PR per hero against `main`. Each scheduled run force-pushes the per-hero branch; the PR is curator-merged or closed at discretion. Direct pushes to `main` are blocked by branch protection.

Shadow runs must not mutate `bazaar_coach` catalog files, open coach PRs, or promote the pipeline phase. `implementation` is the off switch, and `local_dry_run` is the rollback path for artifact-only runs without stats sidecar PRs.

Live cron remains disabled. Do not promote to `live_cron` until all of these are true:
- At least 2 healthy bazaardb patch windows have accumulated.
- At least 60 calendar days of shadow output have accumulated.
- Semantic classifier/provider readiness is confirmed, or the operator records an explicit waiver.
- The curator manually flips `phase`/`dry_run` after reviewing shadow artifacts and rollback behavior.

Current no-LLM shadow artifacts are operational evidence only. They should show `classification_mode: no_llm_shadow`, `semantic_classification: false`, and `llm_provider: none`; candidate labels remain `classification_pending`, not accepted carry/core/support catalog semantics.

The promotion out of `local_dry_run` was validated from a Python 3.12.10 temporary environment. All five supported heroes completed controlled local dry runs with `--mock-llm`, live source fetches, temp-only artifacts, and exit code 0: Dooley, Karnok, Mak, Pygmalien, and Vanessa. Source health was healthy for bazaar-builds.net `2026-W19`, bazaardb `14.0 (Hotfix May 7)`, and Mobalytics meta builds `v541`; this is three healthy sources, not three temporal windows.

Rollback remains explicit: set `phase` back to `local_dry_run` with `dry_run: true` for artifact-only operation, or back to `implementation` to stop fetches, artifacts, stats writes, and PR actions.

## Semantic Classifier Strategy - Open

Before `live_cron` or any catalog-acceptance automation, decide whether to use the existing Claude-backed classifier, add a provider abstraction and alternate hosted provider, use another free/lower-cost provider, or proceed with an explicit operator waiver.

If evaluating a hosted provider, recheck current API availability, billing, quota, model names, data-use terms, and structured JSON reliability at implementation time. ChatGPT Plus/Pro subscriptions do not provide reusable OpenAI API billing for GitHub Actions.

## Review Tooling - On Hold

The pipeline currently emits diff JSON and proposal markdown artifacts, and `live_cron` can maintain one rolling PR per hero when enabled. Broader review tooling, such as a richer dashboard or more detailed per-proposal stats surface, remains deferred until shadow output shows a concrete curator pain point.
