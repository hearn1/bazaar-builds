# Curated game-change signals

Curated game-change signals are source-backed evidence records that increase review pressure for retired, renamed, nerfed, invalidated, or watchlisted cards and items. They live in `game_changes/signals.json` with `schema_version: 1` and are parsed by `automated_builds_pipeline/game_changes.py`.

Signals do not directly mutate player-facing coach catalogs. They are evidence for generated diffs and proposals that curators review before any coach PR is merged.

## Safety model

- Records must be backed by a source before being committed.
- All signals are curator-reviewed before they affect coach PRs.
- No automatic whole-archetype deletion.
- No direct catalog mutation from patch notes or scraped text.
- No hosted LLM requirement in the live path.
- BazaarDB stale absence remains the conservative fallback when no explicit signal exists.
- `pipeline_state.json` must not be mutated by this workflow.

## Source hierarchy

### Tier 1 — preferred official sources

Use these when available:

- Official patch notes
- Official launcher or news posts
- Official site changelogs
- Official data or release notes (when stable and attributable)
- Official Discord announcements (only when linkable or archived with enough provenance)

### Tier 2 — semi-structured or trusted sources (curator review required)

Acceptable with extra scrutiny:

- Structured or semi-structured official exports
- Stable pages with explicit item names and patch or version context
- Trusted community summaries that cite an official source
- Manually archived source snippets committed as evidence

### Tier 3 — supporting context only

Do not use as a primary source for signal records:

- BazaarDB presence or absence
- Mobalytics current presence
- bazaar-builds.net current presence
- Source disagreement summaries
- Historical stats sidecars

### Sources to avoid

Signal records should not be based on:

- Brittle dynamic HTML scraping with unstable selectors
- Pages blocked by Cloudflare or headed-browser requirements in a live path
- Reddit or social speculation without primary-source backing
- Hosted LLM classification
- Image-only patch notes (unless a curator manually transcribes and verifies them)
- Unversioned pages where old text is not preserved

## Signal schema

The document root requires exactly two top-level fields:

| Field | Type | Notes |
|---|---|---|
| `schema_version` | integer | Must be `1` |
| `signals` | list | List of signal objects |

Unknown top-level fields are rejected.

### Required signal fields

| Field | Type | Notes |
|---|---|---|
| `id` | string | Unique identifier for the signal |
| `type` | string | One of the five supported types (see below) |
| `item` | string | Item or card name (case-insensitive matching) |
| `effective_date` | string | ISO 8601 date (`YYYY-MM-DD`) |
| `source_url` | string | URL or stable reference for the source |
| `note` | string | Human-readable summary of the signal |

### Optional signal fields

| Field | Type | Notes |
|---|---|---|
| `hero` | string | Scope to a single hero; omit for global |
| `replacement_item` | string | New item name when an item is renamed |
| `patch` | string | Patch identifier if known |
| `metadata` | object | Flexible key-value annotations (see below) |

Unknown signal fields are rejected. New top-level fields require parser changes to `game_changes.py`. New conventions can be standardized inside `metadata` without bumping the schema.

## Metadata conventions

The `metadata` object is flexible. The following keys are standardized conventions:

| Key | Recommended values | Notes |
|---|---|---|
| `source_kind` | `official_patch_notes`, `official_announcement`, `official_data`, `trusted_summary`, `manual_archive` | Tier of the primary source |
| `source_title` | string | Human-readable title of the source page or document |
| `source_published_at` | ISO 8601 date | When the source was published |
| `source_accessed_at` | ISO 8601 date | When the curator accessed the source |
| `confidence` | `high`, `medium`, `low` | Curator's confidence in the signal |
| `curator` | string | GitHub username of the curator who added the record |
| `status` | `proposed`, `reviewed`, `deprecated` | Curation lifecycle state |
| `source_excerpt` | string | Verbatim excerpt from the source (for provenance) |
| `affected_scope` | `item`, `hero_item`, `archetype`, `global`, `unknown` | Scope of the change |
| `old_item` | string | Original item name before a rename |
| `new_item` | string | New item name after a rename |
| `uncertainty_note` | string | Required for `watchlist` or low-confidence records |

## Signal types

### `removed_card`

Use when a card or item is explicitly removed or no longer obtainable.

Expected behavior:

- Exact support-item records may become narrow item-level removal candidates when safe.
- `carry_items`, `core_items`, `condition_items` records are review-only; they do not imply whole-archetype deletion.
- Current secondary-source presence is surfaced as context.

### `renamed_card`

Use when a source explicitly says an item was renamed.

Expected behavior:

- Use `replacement_item` when the new name is known.
- Treated as migration and review evidence, not a blind global find-and-replace.
- Exact support-item old-name removal may be actionable only when safe; carry/core/condition renames remain review-only.

### `explicit_invalidation`

Use when a source says an old item or build interaction no longer works.

Expected behavior:

- Always requires curator review.
- High priority for `carry_items`, `core_items`, `condition_items` records.
- No automatic deletion unless a future guarded implementation explicitly supports it.

### `major_nerf`

Use when a card still exists but changed enough to make catalog placement questionable.

Expected behavior:

- Review-only.
- No automatic deletion.
- Surfaces affected archetypes and buckets.

### `watchlist`

Use when evidence is credible but incomplete or uncertain.

Expected behavior:

- Review-only.
- No automatic mutation.
- Records should include `metadata.uncertainty_note`.

## Confidence and uncertainty conventions

| Level | When to use |
|---|---|
| `high` | Official, stable, explicit source; item name and change type are clear; patch and effective date are known |
| `medium` | Reliable source but wording, scope, or replacement is incomplete |
| `low` | Watchlist-only; should not create mutation candidates |

Uncertain records should prefer `watchlist` or `explicit_invalidation` over `removed_card`.

## Interaction with BazaarDB stale absence

Explicit signals add source-backed review pressure. They do not replace the 30-day BazaarDB stale-absence model or weaken any fallback safety:

- Current healthy BazaarDB absence is still required for stale absence.
- At least two healthy absence windows are still required.
- Counted absences must span at least 30 days.
- Unhealthy or skipped BazaarDB runs do not count as absence evidence.
- Current secondary-source presence (Mobalytics, bazaar-builds.net) still blocks stale retirement.

## Promoting proposal candidates

Proposal-only candidate records from `game_change_proposals` are unreviewed evidence drafts. To add records to the curated `game_changes/signals.json`, use the promotion workflow.

### Workflow

1. Generate proposal-only candidates from local/manual source material:

   ```bash
   python -m automated_builds_pipeline.game_change_proposals \
     --source-file artifacts/patch_notes.md \
     --candidate-file artifacts/manual_candidates.json \
     --output artifacts/game_change_signal_candidates.json
   ```

2. Review candidate IDs and metadata in the output file.

3. Confirm source provenance, confidence, and uncertainty notes are correct.

4. Preview the promotion output for selected IDs (safe — writes only to the output path):

   ```bash
   python -m automated_builds_pipeline.game_change_promotions \
     --candidate-file artifacts/game_change_signal_candidates.json \
     --signals-file game_changes/signals.json \
     --select proposed-2026-06-01-crystal-blade-removed-card \
     --curator hearn1 \
     --reviewed-at 2026-06-06 \
     --output artifacts/promoted_signals_preview.json
   ```

5. Review the generated preview. Confirm it contains only the selected promoted signal plus existing curated signals, with `metadata.status: reviewed` and correct provenance.

6. Write to `game_changes/signals.json` intentionally (requires `--force`):

   ```bash
   python -m automated_builds_pipeline.game_change_promotions \
     --candidate-file artifacts/game_change_signal_candidates.json \
     --signals-file game_changes/signals.json \
     --select proposed-2026-06-01-crystal-blade-removed-card \
     --curator hearn1 \
     --reviewed-at 2026-06-06 \
     --output game_changes/signals.json \
     --force
   ```

7. Commit curated changes to `game_changes/signals.json` only after human review.

### Promotion requirements

- At least one `--select ID` is required; there is no implicit "promote all" behavior.
- `--curator` (your GitHub username) is required.
- `--reviewed-at` (ISO date of review) is required.
- `--output` is required; existing files are not overwritten without `--force`.
- Selected candidates must have `metadata.status: proposed`.
- Selected candidates must have `metadata.confidence` (`high`, `medium`, or `low`).
- `watchlist` type and `confidence: low` records require `metadata.uncertainty_note`.
- Unresolved `metadata.missing_metadata` blocks promotion by default; pass `--allow-missing-metadata` to override (the field is preserved in the promoted output).
- Duplicate signal IDs or duplicate `(type, hero, item, effective_date)` keys are rejected.
- Final output is validated by the strict signal parser before writing.

### Safety invariants

The promotion workflow does not:

- Fetch live URLs or scrape patch notes.
- Call a hosted LLM.
- Write to coach catalog files.
- Open or update coach PRs.
- Mutate `pipeline_state.json`.
- Automatically promote all candidates — explicit `--select` is always required.
- Change evaluator, applier, or retirement policy behavior.

## Minimal example

```json
{
  "schema_version": 1,
  "signals": [
    {
      "id": "removed-example-item-2026-06-01",
      "type": "removed_card",
      "item": "Example Item",
      "effective_date": "2026-06-01",
      "source_url": "https://www.playthebazaar.com/news/patch-notes-example",
      "note": "Removed in June 2026 patch.",
      "patch": "15.1",
      "metadata": {
        "source_kind": "official_patch_notes",
        "confidence": "high",
        "curator": "hearn1",
        "status": "reviewed"
      }
    }
  ]
}
```
