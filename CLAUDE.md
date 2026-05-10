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

Current local verification: 119 tests passing. Use `python -m pytest -q tests`; a bare repo-root pytest can collect generated `artifacts/` directories and fail before the tracked suite runs.

Fetch evidence for a hero:

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
  --tracker-repo C:\Users\Matt\Desktop\bazaar_coach `
  --stats-dir .\stats `
  --output-dir .\artifacts `
  --classifier-mode no_llm_shadow
```

Use `--mock-llm` only for local synthetic validation. It is not scheduled shadow evidence or catalog-acceptance evidence.

Refresh source-shape samples for curator review:

```powershell
python -m automated_builds_pipeline.research.refresh_samples --source bazaardb
```

## Current Pipeline Phase

`pipeline_state.json` is currently `phase: shadow_cron` with `dry_run: true`. Scheduled shadow runs default to deterministic `no_llm_shadow`; they may fetch sources, evaluate, render diff/proposal artifacts, upload workflow artifacts, and open/update a rolling `automated/stats-sync-<hero>` PR per hero in this repo. The PR is force-pushed on each scheduled run; merge or close at curator discretion. Direct pushes to `main` are not used because branch protection requires PRs.

Do not promote to `live_cron` until at least 2 healthy bazaardb patch windows and at least 60 calendar days of shadow output have accumulated, and semantic classifier/provider readiness is confirmed or explicitly waived. Do not mutate coach catalogs or open coach PRs while `dry_run` remains true.

`implementation` is the off switch. `local_dry_run` is the rollback path for artifact-only scheduled/manual runs without stats sidecar PRs.

## Source Roles

Priority order is bazaardb, then Mobalytics, then bazaar-builds.net.

- `bazaardb.gg/run/meta`: canonical statistical baseline. It is patch-scoped, fetched with Playwright/browser handling because plain HTTP can hit Cloudflare, and healthy absence is the only canonical removal evidence.
- `mobalytics.gg/the-bazaar/guides/meta-builds`: structured editorial source via `window.__PRELOADED_STATE__`; item names are deterministic JSON fields, not LLM-extracted text.
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
automated_builds_pipeline.llm           # classifier modes and hosted provider wiring
automated_builds_pipeline.proposal      # proposal markdown rendering
automated_builds_pipeline.pr_comment    # supporting evidence comment rendering
automated_builds_pipeline.smoke         # source-health smoke workflow support
automated_builds_pipeline.research      # source-shape sample refresh command
```

## Documentation Shape

Keep long-term docs limited to this file, `ROADMAP.md`, and `README.md`. Add temporary docs only for active work that genuinely needs its own in-progress note, and fold durable facts back into the canonical files before deleting the temporary note.
