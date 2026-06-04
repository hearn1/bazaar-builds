# bazaar-builds - Roadmap

Active work tracker. Stable repo context and operator notes live in `AGENTS.md`; user-facing setup and usage live in `README.md`. Completed planning docs are removed rather than kept as historical checklists.

Status labels:
- `Open`: not yet complete.
- `Gate`: manual approval or evidence required before changing phase.
- `On Hold`: deferred until live output or curator need makes the work concrete.

## Automated Builds Pipeline - Live

Current state: `pipeline_state.json` is `phase: live_cron` with `dry_run: false`. The weekly GitHub Actions cron schedule exists (`0 6 * * 5`, Friday 06:00 UTC). Scheduled runs default to the deterministic classifier, fetch live sources, evaluate thresholds, persist stats sidecars through auto-merged `automated/stats-sync-<hero>-<run>` PRs in this repo, and open/update focused additions and retirements proposal PRs in `hearn1/bazaar_coach` when there are non-empty catalog changes. Direct pushes to `main` remain blocked by branch protection.

The live promotion intentionally bypasses the original launch-timing thresholds:
- **Gate 1**: at least 2 counted healthy bazaardb patch windows.
- **Gate 2**: at least 14 calendar days of deterministic classifier output.

The decision is recorded in `waivers/live_cron_promotion_waiver_2026-05-18.md`, and `readiness.py` reports that waiver in both the summary and warnings. This is an explicit operator waiver, not a silent threshold reduction. The waiver does not bypass deterministic classifier readiness, recent malformed/unhealthy bazaardb checks, schema validation, or curator review of coach PRs.

Gate 3 is satisfied by the no-LLM `DeterministicClassifier`: it produces real carry/core/support tiers, and `readiness.py` derives `classifier_ready` from sidecar `last_classifier_mode`. Hosted LLM classification is removed from the live posture; do not require `CLAUDE_API_KEY`, Anthropic credentials, or hosted classifier/provider readiness.

Deterministic artifacts carry accepted semantics: `classification_mode: deterministic`, `semantic_classification: true`, `classifier_provider: deterministic`, with candidate items in `candidate_core`/`candidate_support` rather than `candidate_pending`. Legacy `no_llm_shadow` artifacts (`classification_mode: no_llm_shadow`, `semantic_classification: false`, `classifier_provider: none`, `classification_pending` labels) remain operational evidence only and are not scheduled live evidence.

Live promotion verification on 2026-05-18 used Python 3.14.4 on this workstation. The tracked suite passed with `171 passed`. Deterministic `local_dry_run` verification completed for Karnok, Jules, and Stelle with temporary state, temporary stats, temporary artifacts, and exit code 0 for each hero. The sibling coach checkout had no catalog mutations from verification.

Rollback remains explicit: set `phase` back to `local_dry_run` with `dry_run: true` for artifact-only operation, or back to `implementation` to stop fetches, artifacts, stats writes, and PR actions.

## Retirement Rollout - Gate

Retirement automation is live-path code, but removals remain intentionally gated by evidence, partitioning, and curator review. A stale retirement requires current healthy BazaarDB absence, at least 2 counted healthy BazaarDB absence windows, and a 30 calendar day absence span. Unhealthy or skipped BazaarDB runs do not count. Current Mobalytics or bazaar-builds.net presence blocks retirement, while older secondary-source sightings are retained only as supporting context.

Curated game-change signals are the only explicit patch-note override path. They are maintained in `game_changes/signals.json` as schema version 1 records with source-backed `source_url`, `note`, and optional structured `metadata`; supported types are `removed_card`, `renamed_card`, `explicit_invalidation`, `major_nerf`, and `watchlist`. Missing default signals load empty. Explicit configured signal paths fail closed if missing, malformed, or unsupported. No hosted LLM or scraping step is part of this workflow.

Bucket behavior is the current risk boundary. Exact archetype `support_items` removals are applier-supported. `carry_items`, `core_items`, and `condition_items` produce review candidates with affected build context and no whole-archetype deletion. `universal_utility_items` and `economy_items` are support-like metadata, but remain review-only until a later applier expansion. Active global or hero removal freezes mark removable support rows as blocked and keep all retirement evidence review-only.

Coach catalog proposals are partitioned by intent. Additions use `pipeline/<hero>-additions`; retirements use `pipeline/<hero>-retirements`. Mixed diffs can open both PRs from the same captured coach base commit. Retirement partitions with only review-only evidence or skipped rows do not create empty PRs, but their skip reasons are logged for operator review.

Rollout gates before broad retirement trust:
- Run `python -m pytest -q tests` for the tracked local suite. Do not use bare repo-root pytest.
- For higher-risk retirement changes or before expanding applier scope, run the 7-hero `local_dry_run` sweep with temporary state and compare adds, updates, weaker signals, applied rows, and skipped rows before/after.
- Confirm no coach catalogs or `card_cache_names.txt` are vendored into this repo; use the sibling `../bazaar_tracker` or `bazaar_coach` checkout.
- Keep `pipeline_state.json` unchanged unless Matt explicitly approves a phase or freeze-state change.

## Semantic Classifier Strategy - Open

Hosted LLM classification has been removed because it adds recurring API cost and is not needed for the deterministic single-patch formulation. Catalog formulation should use deterministic within-patch evidence strength from bazaardb statistics, Mobalytics structured editorial data, and same-window source agreement.

## Review Tooling - On Hold

The pipeline emits diff JSON and proposal markdown artifacts, and `live_cron` maintains focused rolling coach PRs per hero when there are proposed catalog changes. Broader review tooling, such as a richer dashboard or more detailed per-proposal stats surface, remains deferred until live output shows a concrete curator pain point.
