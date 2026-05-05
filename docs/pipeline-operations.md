# Automated Builds Pipeline Operations

This is the interim curator runbook for the automated builds refresh pipeline. Subtask 7 is expected to add the longer-term review tooling experience; until then, the workflow emits proposal artifacts or rolling tracker PRs.

## Phase Progression

The workflow reads `pipeline_state.json` from the bazaar-builds repo. To move phases, edit the `phase` field and keep `dry_run` aligned:

```json
{
  "phase": "local_dry_run",
  "dry_run": true
}
```

Use this progression:

1. `local_dry_run`: run locally or by manual dispatch; writes artifacts only.
2. `shadow_cron`: weekly cron runs and uploads artifacts to the workflow run; no tracker PRs open.
3. `live_cron`: weekly cron updates one rolling PR per hero when the diff is non-empty.

Do not move to `live_cron` until shadow output has at least 6 healthy bazaardb patch windows and at least 60 days of calendar observation. The flip is manual even after the thresholds are met.

`implementation` is the off switch. In that phase, the workflow exits successfully with no fetches, artifacts, or PR actions.

## Manual Freeze

On patch days, set a global removal freeze:

```json
{
  "freeze_removals_until": "2026-05-18"
}
```

For a hero-specific freeze, use `hero_freezes`:

```json
{
  "hero_freezes": {
    "Karnok": {
      "freeze_removals_until": "2026-05-18",
      "notes": "Post-patch freeze for Karnok item changes"
    }
  }
}
```

The workflow still runs during a freeze. The evaluator suppresses removal proposals while add proposals continue.

## Local Dry Run

From the bazaar-builds repo:

```powershell
python -m automated_builds_pipeline.pipeline run `
  --hero Karnok `
  --state-file pipeline_state.json `
  --tracker-repo C:\Users\Matt\Desktop\bazaar_tracker `
  --stats-dir .\stats `
  --output-dir .\artifacts `
  --api-key-env CLAUDE_API_KEY
```

Use `--no-bazaardb` when you intentionally want to skip bazaardb for a local run. That marks the source as `skipped` with `operator skip`.

## Shadow Artifacts

In `shadow_cron`, the workflow uploads artifacts named:

```text
automated-builds-<hero>-<run_id>
```

Each artifact contains `<hero>_diff.json` and `<hero>_build_update_proposal.md` from that run.

## PAT Rotation

`TRACKER_PR_TOKEN` is a fine-grained PAT scoped only to `hearn1/bazaar_tracker` with:

- Contents: Read & Write
- Pull requests: Read & Write

Rotate it about every 90 days, immediately after suspected exposure, or when the owning account's access changes. No workflow or repo-write permissions are required.

## Live PR Behavior

In `live_cron`, each non-empty hero diff force-pushes `pipeline/<hero>` in `bazaar_tracker` and opens or updates the matching rolling PR:

```text
[automated-builds] <hero> proposal
```

Empty diffs short-circuit and do not push. The pipeline never auto-merges; curator review remains the final gate.

## Current Unresolveds

Stats sidecars are written by the pipeline to the configured `--stats-dir`. The current workflow keeps them in the run workspace; a later operations decision should pin whether those bot-written stats are committed back to bazaar-builds directly or preserved only through workflow artifacts.
