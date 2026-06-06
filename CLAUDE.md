# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Curator toolchain for The Bazaar build catalogs consumed by `bazaar_coach`. This repo owns evidence gathering, threshold evaluation, proposal rendering, source-drift checks, stats sidecars, and future rolling coach PR automation. The coach repo remains the source of truth for player-facing `<hero>_builds.json` catalogs and `builds_schema.json`.

## Common Commands

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest -q tests
```

Use `python -m pytest -q tests`; a bare repo-root pytest can collect generated `artifacts/` directories and fail before the tracked suite runs.

Validate pipeline changes safely:
- `python -m pytest -q tests` — fixture golden gate (network-free; CI gate). The `vanessa_diving_helmet` fixture is `xfail(strict=True)` until #131 lands.
- `python artifacts/sweep_dry_run.py` — 7-hero `local_dry_run` smoke (requires network). Uses a temp state file; NEVER reads or mutates `pipeline_state.json`, `stats/`, or `../bazaar_coach`. See README "Validating pipeline changes safely" for full instructions.

Fetch evidence for a hero. The GitHub repo is `hearn1/bazaar_coach`, but the
local working checkout in use is the sibling directory `..\bazaar_coach`;
point `--catalog-dir`/`--names-file`/`--tracker-repo` at that local path:

```powershell
python bazaar_build_enricher.py https://bazaar-builds.net/category/builds/karnok-builds/ `
  --hero Karnok --days 30 --fetch-posts `
  --catalog-dir ..\bazaar_coach `
  --names-file ..\bazaar_coach\card_cache_names.txt `
  --output artifacts\karnok_bazaar_builds_summary.json
```

Compare an artifact against a coach catalog:

```powershell
python bazaar_build_enricher.py compare `
  artifacts\karnok_bazaar_builds_summary.json `
  ..\bazaar_coach\karnok_builds.json `
  --output artifacts\karnok_build_update_proposal.md
```

Run the automated pipeline locally:

```powershell
python -m automated_builds_pipeline.pipeline run `
  --hero Karnok `
  --state-file pipeline_state.json `
  --tracker-repo ..\bazaar_coach `
  --stats-dir .\stats `
  --output-dir .\artifacts `
  --classifier-mode deterministic
```

Use `--classifier-mode no_llm_shadow` only for observation-only dry-run artifacts that deliberately skip semantic carry/core/support classification.

Refresh source-shape samples for curator review:

```powershell
python -m automated_builds_pipeline.research.refresh_samples --source bazaardb
```

## Current Pipeline Phase

`pipeline_state.json` is currently `phase: live_cron` with `dry_run: false`. Scheduled runs default to deterministic classification; they fetch sources, evaluate, persist stats sidecars through auto-merged `automated/stats-sync-<hero>-<run>` PRs in this repo, and open/update rolling proposal PRs in `hearn1/bazaar_coach` when there are non-empty catalog changes. Direct pushes to `main` are not used because branch protection requires PRs.

The live promotion was made with an explicit operator waiver (`waivers/live_cron_promotion_waiver_2026-05-18.md`) that bypasses Gate 1 (2 counted healthy bazaardb patch windows) and Gate 2 (14 calendar days of deterministic classifier output). The waiver does not bypass deterministic classifier readiness, recent malformed/unhealthy bazaardb checks, schema validation, or curator review of coach PRs.

`implementation` is the off switch. `local_dry_run` is the rollback path for artifact-only scheduled/manual runs without stats sidecar PRs or coach catalog mutation.

## Source Roles

Priority order is bazaardb, then Mobalytics, then bazaar-builds.net.

- `bazaardb.gg/run/meta`: canonical statistical baseline. It is patch-scoped, fetched with Playwright/browser handling because plain HTTP can hit Cloudflare, and healthy absence is the only canonical removal evidence.
- `mobalytics.gg/the-bazaar/guides/meta-builds`: structured editorial source via `window.__PRELOADED_STATE__`; item names are deterministic JSON fields, not free-text extraction.
- `bazaar-builds.net/category/builds/`: dated WordPress post evidence. Use `--fetch-posts`; the 30-day window depends on parsed post dates and a non-empty known-items list.

Unhealthy source runs contribute no absence evidence. Multiple healthy sources in one run are not multiple temporal windows.

## Stats Sidecars

Stats sidecars live in `stats/<hero_slug>_stats.json` and are pipeline state, not player-facing catalog data. They are committed by `shadow_cron` and `live_cron`, never by `local_dry_run`. The sidecar schema is versioned at `schema_version: 1`; readers should fail closed on newer versions.

Sidecar writes use temp-file plus replace semantics. Workflow concurrency is expected to prevent overlapping writes; the sidecar layer does not add file locking.

## Catalog Contract

`bazaar_coach` owns `<hero>_builds.json` and `builds_schema.json`. This repo validates generated catalogs against the coach schema but should not vendor coach catalogs or `card_cache_names.txt`. Always pass `--catalog-dir` and `--names-file` to point at the live coach checkout.

When manually curating, distinguish no-op workflow validation from evidence-bearing catalog validation. Catalog curation needs fetched post evidence, normally via `--fetch-posts`, or an explicitly evidence-backed empty result after fetch attempts.

## Architecture

```text
bazaar_build_enricher.py                # manual source fetch and compare workflow
automated_builds_pipeline.pipeline      # phase-aware automated run orchestration
automated_builds_pipeline.sources.*     # source fetchers and health checks
automated_builds_pipeline.evaluator     # threshold rows and source-disagreement logic
automated_builds_pipeline.stats         # per-hero stats sidecar read/write API
automated_builds_pipeline.diff          # candidate diff construction
automated_builds_pipeline.classification # shared classifier result types
automated_builds_pipeline.proposal      # proposal markdown rendering
automated_builds_pipeline.pr_comment    # supporting evidence comment rendering
automated_builds_pipeline.smoke         # source-health smoke workflow support
automated_builds_pipeline.research      # source-shape sample refresh command
```

## Documentation Shape

Keep long-term docs limited to this file, `ROADMAP.md`, and `README.md`. Add temporary docs only for active work that genuinely needs its own in-progress note, and fold durable facts back into the canonical files before deleting the temporary note.
