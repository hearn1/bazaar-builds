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

The GitHub Actions workflow already has a weekly cron schedule (`0 6 * * 0`
on Sundays) plus manual dispatch. Phase state controls what a scheduled run is
allowed to do; it does not create or remove the schedule.

Use this progression:

1. `local_dry_run`: scheduled or manual dry runs may fetch sources, evaluate, write diff/proposal artifacts, and upload those artifacts; stats sidecars are not saved or committed.
2. `shadow_cron`: weekly cron runs do the same artifact work and additionally save and commit `stats/<hero>_stats.json` sidecars in bazaar-builds; tracker PR/catalog mutation remains disabled.
3. `live_cron`: weekly cron updates one rolling PR per hero when the diff is non-empty.

The pipeline is currently promoted to `local_dry_run` only. Do not move to `shadow_cron` until the entry criteria below are satisfied, the deterministic no-LLM shadow strategy has been reviewed from workflow wiring through artifacts, and the curator explicitly flips `pipeline_state.json`. Do not move to `live_cron` until shadow output has at least 6 healthy bazaardb patch windows, at least 60 days of calendar observation, and later semantic catalog review. Each flip is manual even after the thresholds are met.

`implementation` is the off switch. It is the only phase that makes the scheduled workflow exit successfully inside the pipeline before source fetches, artifact writes, stats writes, or PR actions.

The current `local_dry_run` state was validated from a Python 3.12.10 temporary environment with focused tests passing (`59 passed in 0.39s`). All five supported heroes completed controlled local dry runs with `--mock-llm`, live source fetches, temp-only artifacts, and exit code 0: Dooley, Karnok, Mak, Pygmalien, and Vanessa. Each hero produced diff JSON and proposal markdown, no real LLM/API calls occurred, and no checked-in pipeline state, catalog, stats, or tracker files mutated. That validation remains local synthetic validation only; scheduled `shadow_cron` now uses `--classifier-mode no_llm_shadow` unless manually overridden.

Source health was healthy for these three sources during validation: `bazaar_builds_net:2026-W19`, `bazaardb:14.0 (Hotfix May 7)`, and `mobalytics_meta_builds:v541`. This is three healthy sources, not three temporal windows. Markdown source-health tables are review summaries; the run's diff JSON is the more complete artifact for source-health review when details, skipped-source diagnostics, or per-source observations matter.

Artifact review found the mock-mode proposals operationally valid: fetch, evaluation, mock classification, diff rendering, and proposal rendering all completed. They are not catalog-acceptance evidence. Deterministic no-LLM shadow outputs are marked `classification_mode: no_llm_shadow`, `semantic_classification: false`, and `llm_provider: none`; candidate item labels remain `classification_pending` rather than carry/core/support conclusions. Duplicate/near-duplicate proposals and pending classifications are normal curator review items in no-LLM shadow mode, not pipeline failures.

### `shadow_cron` Entry Criteria

Advance from `local_dry_run` to `shadow_cron` only when all of the following are true:

- All supported-hero local dry-run artifacts have been reviewed for operational validity.
- Source-health output clearly represents required fields for each required source, including source name, status, window or patch identifier, and diagnostic details when unhealthy/skipped.
- The local dry-run evidence shows no checked-in mutation of pipeline state, catalog JSON, tracker files, generated artifacts, or stats sidecars.
- The curator understands that `shadow_cron` starts persisting `stats/<hero>_stats.json` sidecars in bazaar-builds. Those commits are bot provenance for threshold history, not catalog changes.
- The deterministic/no-LLM shadow classifier strategy is reviewed before promotion. Scheduled `shadow_cron` defaults to `--classifier-mode no_llm_shadow`, which writes observation artifacts without hosted LLM/API classification.
- The curator understands that no-LLM shadow artifacts are operational evidence only: they do not validate carry/core/support semantics, authorize catalog mutation, promote `pipeline_state.json`, or make pending candidates accepted catalog candidates.
- The curator understands that `--mock-llm` remains only local synthetic validation. It is manually selected, emits `classification_mode: mock`, and should not be treated as scheduled shadow evidence or semantic catalog-acceptance evidence.
- Rollback is clear: set `phase` back to `local_dry_run` with `dry_run: true` for artifact-only operation, or back to `implementation` to stop fetches, artifacts, stats writes, and PR actions.

Before flipping to `shadow_cron`:

- Confirm the existing Actions schedule on `main` is intended to run weekly.
- Confirm scheduled `shadow_cron` should continue using deterministic/no-LLM classification, or record an explicit operator waiver for a different classifier strategy.
- If a hosted LLM classifier is selected for a manual run or future phase, confirm the required secret readiness, API availability, and cost tolerance for that provider.
- Confirm bot-authored stats sidecar commits to bazaar-builds are accepted for all scheduled heroes.
- Validate the chosen classifier strategy from the workflow path, including artifact metadata, pending classification semantics, and the proposal warning.
- Confirm rollback path and operator availability: `local_dry_run` keeps artifact-only scheduled/manual runs, while `implementation` stops fetches, artifacts, stats writes, and PR actions.

### Post-Merge `shadow_cron` Promotion Gate

Issue 1 must leave `pipeline_state.json` unchanged. Promoting from
`local_dry_run` to `shadow_cron` is a separate rollout decision after the Issue
1 PR merges.

Use this checklist before the promotion PR or manual state flip:

- Issue 1 PR is merged to `main`.
- The scheduled workflow has been reviewed, including the weekly cron schedule,
  manual dispatch inputs, phase override behavior, and stats persistence step.
- Scheduled `shadow_cron` still defaults to deterministic/no-LLM classification
  with `--classifier-mode no_llm_shadow`.
- No hosted provider key is required for the default `shadow_cron` path.
- Stats sidecar commit behavior is understood: `shadow_cron` can commit
  `stats/<hero>_stats.json` in bazaar-builds, but tracker catalog mutation and
  tracker PR creation remain disabled.

Monitor the first scheduled `shadow_cron` run for:

- Source health by provider and temporal window.
- Stats sidecar diffs and bot commit provenance.
- Generated diff JSON and proposal markdown artifacts.
- Artifact metadata showing no provider calls: `classification_mode:
  no_llm_shadow`, `semantic_classification: false`, and `llm_provider: none`.
- No mutation of tracker catalog files.
- No tracker PR creation.

Do not conclude from the first no-LLM shadow run that:

- Carry/core/support semantics are accepted.
- The live catalog is ready for mutation.
- `live_cron` should be enabled automatically.

Hold promotion or roll back to `local_dry_run` if any of the following occur:

- Source fetch failures span multiple providers.
- Any unexpected catalog or pipeline-state mutation appears.
- The workflow attempts a hosted provider call in default `shadow_cron`.
- Stats sidecars contain semantic carry/core/support conclusions instead of
  deterministic observation history.
- Artifact metadata is missing no-LLM markers.

### Temporal Source-Health Windows

Do not treat multiple healthy sources in one run as multiple temporal windows. A temporal window is one successful observation period for a given source over time.

- For bazaar-builds.net, a healthy temporal window is a healthy fetch for a dated 30-day/window identifier such as `2026-W19`.
- For bazaardb, a healthy temporal window is a healthy fetch for a distinct patch label, such as `14.0 (Hotfix May 7)`.
- For Mobalytics meta builds, a healthy temporal window is a healthy fetch for a distinct document/version identifier such as `v541`.

Later `shadow_cron` and `live_cron` evidence should describe both source count and temporal-window count explicitly. The live gate remains stricter than generic source health: at least 6 healthy bazaardb patch windows and at least 60 calendar days of shadow output are required before enabling rolling tracker PRs.

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

Use `--mock-llm` for local dry-run validation without real LLM/API calls. Mock mode emits synthetic low-confidence support classifications and is labeled `classification_mode: mock`; it is not scheduled shadow evidence. For deterministic shadow observation without a provider, use `--classifier-mode no_llm_shadow`, which emits `classification_mode: no_llm_shadow`, `semantic_classification: false`, `llm_provider: none`, and `classification_pending` item labels. Scheduled `shadow_cron` runs default to `no_llm_shadow` unless a manual dispatch classifier mode overrides it. Source fetches are still live unless you also pass source-disabling flags such as `--no-bazaardb`, which marks the source as `skipped` with `operator skip`.

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
threshold evaluator, not human-review catalog changes. The persist step runs on
`main` in `shadow_cron` and `live_cron`; it does not run in `local_dry_run`.

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
successful `shadow_cron` or `live_cron` pipeline run on `main`, so multi-window
thresholds accumulate across cron runs. They are intentionally not saved or
committed in `local_dry_run`. The deterministic/no-LLM shadow classifier path
does not resolve catalog acceptance semantics; carry/core/support validation
requires a semantic classifier or manual review in a later phase.
