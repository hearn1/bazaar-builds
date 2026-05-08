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

The pipeline is currently promoted to `local_dry_run` only. Do not move to `shadow_cron` until local dry-run validation remains clean across the selected heroes and source-health accumulation is ready to begin. Do not move to `live_cron` until shadow output has at least 6 healthy bazaardb patch windows and at least 60 days of calendar observation. Each flip is manual even after the thresholds are met.

`implementation` is the off switch. In that phase, the workflow exits successfully with no fetches, artifacts, or PR actions.

The `local_dry_run` promotion was validated from a Python 3.12.10 temporary environment with focused tests passing (`61 passed`), a Karnok `--mock-llm` run exiting 0, and live source fetches reporting healthy windows for bazaar-builds.net (`2026-W19`), bazaardb (`14.0 (Hotfix May 7)`), and Mobalytics (`v541`). The run produced temp-space `Karnok_diff.json` and `Karnok_build_update_proposal.md` artifacts only; it did not create a stats sidecar or mutate checked-in catalog, stats, or tracker files.

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
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

```powershell
python -m automated_builds_pipeline.pipeline run `
  --hero Karnok `
  --state-file pipeline_state.json `
  --tracker-repo C:\Users\Matt\Desktop\bazaar_tracker `
  --stats-dir .\stats `
  --output-dir .\artifacts `
  --api-key-env CLAUDE_API_KEY
```

Use `--mock-llm` for dry-run validation without real LLM/API calls. Source fetches are still live unless you also pass source-disabling flags such as `--no-bazaardb`, which marks the source as `skipped` with `operator skip`.

For ad-hoc state files in temp space, write BOM-free JSON. In Windows PowerShell 5.1, prefer the .NET UTF-8 constructor over `Set-Content -Encoding UTF8`, which writes a BOM:

```powershell
$state = '{"phase":"local_dry_run","dry_run":true,"schema_version":1}'
[System.IO.File]::WriteAllText($env:TEMP + '\pipeline_state.json', $state, [System.Text.UTF8Encoding]::new($false))
```

## Shadow Artifacts

In `shadow_cron`, the workflow uploads artifacts named:

```text
automated-builds-<hero>-<run_id>
```

Each artifact contains `<hero>_diff.json` and `<hero>_build_update_proposal.md` from that run.

Stats sidecars are persisted in the bazaar-builds repo under `stats/` as
`stats/<hero>_stats.json`. These commits are bot-written provenance for the
threshold evaluator, not human-review catalog changes.

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

After the PR body is created or edited, `live_cron` also posts a supporting-evidence comment rendered from the just-saved stats sidecar and the run's diff JSON. The comment shows recent per-window item history for each proposed add/remove and updates in place by trying `gh pr comment --edit-last` before falling back to a new `gh pr comment` on the first run.

## Current Unresolveds

Stats sidecars are committed back to bazaar-builds by the workflow after each
successful pipeline run, so multi-window thresholds accumulate across cron
runs.
