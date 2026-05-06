# Stats Sidecar Schema

*Subtask 2 deliverable for the Automated Builds Refresh Pipeline. This file
defines the sidecar persisted in `bazaar-builds`, not the player-facing
`bazaar_tracker` catalogs.*

---

## 1. File Ownership

One file per hero:

```text
<stats-dir>/<hero_slug>_stats.json
```

`hero_slug` is the hero display name case-folded with non-alphanumeric runs
replaced by `_` (`Karnok` -> `karnok_stats.json`).

For the live pipeline, the canonical storage location is the repo-root
`stats/` directory in `bazaar-builds`; cron writes
`stats/<hero_slug>_stats.json` there and commits the updated sidecar.

The file is pipeline state. It is never embedded in or used to mutate
`<hero>_builds.json`.

---

## 2. Top-Level Shape

```json
{
  "schema_version": 1,
  "hero": "Karnok",
  "generated_at": "2026-05-05T12:00:00Z",
  "retention_windows": {
    "bazaardb": null,
    "mobalytics_meta_builds": 26,
    "mobalytics_build_articles": 26,
    "bazaar_builds_net": 26,
    "in_house_tracker": 26
  },
  "source_windows": {
    "bazaardb": [
      {
        "window_id": "bazaardb:Apr 29",
        "observed_at": "2026-05-05T12:00:00Z",
        "health_status": "healthy",
        "artifact_ref": "artifacts/bazaardb/karnok-2026-05-05.json",
        "details": []
      }
    ]
  },
  "items": {}
}
```

Field contract:

Nullable optional fields may be omitted by writers when their value is `null`.

| Field | Type | Notes |
|---|---|---|
| `schema_version` | integer | Starts at `1`. Readers refuse newer versions. |
| `hero` | string | Hero display name. |
| `generated_at` | string | UTC ISO-8601 timestamp for the last write. |
| `retention_windows` | object | Per-source max retained windows. `null` means unbounded until a later disk-pressure cap is chosen. |
| `source_windows` | object | Per-source run/window summaries. |
| `items` | object | Item histories keyed by item display name. |

Supported source enum:

```text
bazaardb
mobalytics_meta_builds
mobalytics_build_articles
bazaar_builds_net
in_house_tracker
```

---

## 3. Window IDs

Window IDs are source-prefixed strings. They must begin with the source enum and
`:`. The suffix is source-native and stable for that source's cadence.

| Source | Format | Example |
|---|---|---|
| `bazaardb` | `bazaardb:<patch-label>` | `bazaardb:Apr 29` |
| `mobalytics_meta_builds` | `mobalytics_meta_builds:v<document-version>` | `mobalytics_meta_builds:v537` |
| `mobalytics_build_articles` | `mobalytics_build_articles:<article-slug-or-version>` | `mobalytics_build_articles:tortuga-vanessa-kripp-build` |
| `bazaar_builds_net` | `bazaar_builds_net:<iso-week>` | `bazaar_builds_net:2026-W18` |
| `in_house_tracker` | `in_house_tracker:<run-id-or-date>` | `in_house_tracker:2026-05-05` |

Subtask 1's threshold output may expose these same strings. The sidecar treats
them as source-window identity and does not parse the suffix.

---

## 4. Per-Item Shape

The sidecar is a single hero file with nested per-source history under each
item. It is not one file per `(hero, source)`.

```json
{
  "items": {
    "Hunting Knife": {
      "per_source": {
        "bazaar_builds_net": {
          "first_seen_window": "bazaar_builds_net:2026-W17",
          "last_seen_window": "bazaar_builds_net:2026-W18",
          "windows_seen": 2,
          "windows_observed": 2,
          "per_window": [
            {
              "window_id": "bazaar_builds_net:2026-W18",
              "observed_at": "2026-05-05T12:00:00Z",
              "present": true,
              "phase": "late",
              "archetype": "Axe",
              "appearances": 5,
              "sample_count": 7,
              "frequency": 0.714,
              "rank": null,
              "archetypes_seen": ["Axe"],
              "evidence_refs": [
                "artifacts/bazaar_builds_net/karnok-2026-W18.json"
              ],
              "metadata": {}
            }
          ]
        }
      }
    }
  }
}
```

Field contract:

Nullable optional row fields may be omitted by writers when their value is
`null`.

| Field | Type | Notes |
|---|---|---|
| `first_seen_window` | string or null | First retained window where `present` is true. |
| `last_seen_window` | string or null | Last retained window where `present` is true. |
| `windows_seen` | integer | Count of retained rows with `present: true`. |
| `windows_observed` | integer | Count of retained rows for the item/source. |
| `per_window[]` | array | Retained item evidence rows, oldest to newest. |
| `per_window[].window_id` | string | Source-prefixed window ID. |
| `per_window[].observed_at` | string | UTC ISO-8601 timestamp. |
| `per_window[].present` | boolean | Presence in that source window. |
| `phase` | string or null | Catalog phase when known. |
| `archetype` | string or null | Matching catalog archetype or normalized source tag. |
| `appearances` | integer or null | Raw source appearance count when available. |
| `sample_count` | integer or null | Source denominator when available. |
| `frequency` | number or null | `appearances / sample_count` when meaningful. |
| `rank` | integer or null | Source rank when available, especially bazaardb. |
| `archetypes_seen[]` | array of strings | Source archetype/tag names that cited the item. |
| `evidence_refs[]` | array of strings | Artifact pointers, not embedded raw scrape data. |
| `metadata` | object | Source-specific small fields; avoid raw payload dumps. |

Rows may record `present: false` for evaluated catalog items. These rows count
toward `windows_observed` but not `windows_seen`.

---

## 5. Retention

Retention is per source and configurable by `retention_windows`.

Defaults:

| Source | Default |
|---|---:|
| `bazaardb` | `null` |
| `mobalytics_meta_builds` | `26` |
| `mobalytics_build_articles` | `26` |
| `bazaar_builds_net` | `26` |
| `in_house_tracker` | `26` |

`26` windows is approximately six months at weekly cadence for
`bazaar_builds_net`. `bazaardb` keeps all patches since pipeline inception by
default; if sidecars become noisy or disk pressure appears, set an explicit cap
without changing the schema.

When a cap is set, appending a window drops the oldest retained rows for that
source. Retention applies independently per source, so appending
`bazaardb` data cannot trim `mobalytics_meta_builds`.

---

## 6. Atomicity

Writes use temp-file plus rename in the same directory:

1. Serialize the full JSON file.
2. Write to `.<target>.<uuid>.tmp` in the target directory.
3. Flush and `fsync` the temp file.
4. Rename with `os.replace`.
5. `fsync` the directory where the platform supports it.
6. Remove any leftover temp file on failure.

Failure modes:

| Failure | Result |
|---|---|
| Serialization fails | Existing sidecar is untouched. |
| Temp write or fsync fails | Existing sidecar is untouched; temp file is removed if present. |
| Failure before rename | Existing sidecar is untouched; temp file is removed. |
| Process crash after rename | New file may be present; directory fsync reduces but cannot eliminate platform-specific durability risk. |

Concurrency assumption: GitHub Actions concurrency control from the pipeline
workflow prevents overlapping writes for the same branch/run family. The sidecar
does not implement file locking.

---

## 7. Reader Contract

Readers refuse unknown future schemas. A reader that supports
`schema_version: 1` must fail closed on `schema_version: 2` instead of trying to
load a shape it may not understand.

The threshold evaluator should query histories by `(hero, item, source, N)`.
The sidecar API returns the last `N` retained per-window rows in oldest-to-newest
order for that item/source.
