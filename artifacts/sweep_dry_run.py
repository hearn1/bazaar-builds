"""7-hero local_dry_run sweep — safe smoke for pipeline broadening work.

Runs the full pipeline in ``local_dry_run`` mode for each of the 7 primary
heroes and prints a combined before/after table of proposed changes plus
applier outcomes.

SAFETY CONTRACT
===============
- Writes a **temporary** state file with ``phase: local_dry_run``.  The live
  ``pipeline_state.json`` is NEVER read or mutated by this script.
- In ``local_dry_run``, ``pipeline.run`` skips ``append_window``/``save_stats``
  (guarded at ``pipeline.py:91``) and returns before any git/PR action
  (guarded at ``pipeline.py:109-111``).  No stats sidecar is written; no
  tracker checkout is modified.
- Stats are loaded read-only from the real ``stats/`` directory so the sweep
  reflects current patch history.
- Diffs are written to ``artifacts/verify/<Hero>_diff.json`` (ignored by git
  under ``artifacts/verify/``).

Usage
-----
  python artifacts/sweep_dry_run.py [--hero Hero1 Hero2 ...]
                                    [--tracker-repo PATH]
                                    [--stats-dir PATH]
                                    [--output-dir PATH]
                                    [--no-bazaardb]

Defaults: all 7 heroes, tracker at ``../bazaar_coach``, stats at
``./stats``, output at ``./artifacts/verify``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent.parent  # repo root

# Running this file as a script puts artifacts/ on sys.path[0], not the repo
# root, so ``import automated_builds_pipeline`` fails.  Insert the repo root so
# the documented invocation (``python artifacts/sweep_dry_run.py``) works
# without requiring ``PYTHONPATH=.``.
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Default Windows consoles use cp1252, which cannot encode the table glyphs
# (→, Δ) below.  Switch stdout/stderr to utf-8 when the stream supports it so
# the documented invocation runs cleanly without ``PYTHONIOENCODING=utf-8``.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass

HEROES = ["Dooley", "Jules", "Karnok", "Mak", "Pygmalien", "Stelle", "Vanessa"]

_LOCAL_DRY_RUN_STATE = {
    "schema_version": 1,
    "phase": "local_dry_run",
    "dry_run": False,
    "patch_label": None,
    "expected_bazaardb_patch_label": None,
    "freeze_removals_until": None,
    "hero_freezes": {},
}

_DIVIDER = "-" * 72


def _write_temp_state(tmp_dir: str) -> Path:
    path = Path(tmp_dir) / "sweep_local_dry_run_state.json"
    path.write_text(json.dumps(_LOCAL_DRY_RUN_STATE, indent=2), encoding="utf-8")
    return path


def _run_hero(
    hero: str,
    *,
    state_file: Path,
    tracker_repo: Path,
    stats_dir: Path,
    output_dir: Path,
    no_bazaardb: bool,
    promote_cross_source: bool = False,
) -> dict[str, Any]:
    """Run pipeline for one hero in local_dry_run; return the diff JSON."""
    # Import here so the module is only needed when actually running the sweep
    # (not during import of this script by the safety test).
    from automated_builds_pipeline.pipeline import run as pipeline_run

    result = pipeline_run(
        hero=hero,
        state_file=state_file,
        tracker_repo=tracker_repo,
        stats_dir=stats_dir,
        output_dir=output_dir,
        no_bazaardb=no_bazaardb,
        classifier_mode="deterministic",
        promote_cross_source=promote_cross_source,
    )
    if result.diff_path and result.diff_path.exists():
        return json.loads(result.diff_path.read_text(encoding="utf-8"))
    return {}


def _summarize(diff: dict[str, Any]) -> dict[str, int]:
    pc = diff.get("proposed_changes", {})
    return {
        "adds": len(pc.get("archetype_additions", [])),
        "updates": len(pc.get("archetype_updates", [])),
        "arch_removes": len(pc.get("archetype_removal_candidates", [])),
        "item_removes": len(pc.get("item_removal_candidates", [])),
        "weaker": len(diff.get("weaker_signals", [])),
    }


def _apply_summary(diff: dict[str, Any], tracker_repo: Path, hero: str) -> dict[str, int]:
    """Run the applier against the diff and catalog (read-only deep copy)."""
    from automated_builds_pipeline.applier import apply_proposed_changes

    catalog_path = tracker_repo / "builds" / f"{hero.lower()}_builds.json"
    if not catalog_path.exists():
        catalog_path = tracker_repo / f"{hero.lower()}_builds.json"
    if not catalog_path.exists():
        return {"applied": 0, "skipped": 0, "changed": False, "error": "catalog not found"}

    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        result = apply_proposed_changes(catalog, diff)
        return {
            "applied": len(result.applied),
            "skipped": len(result.skipped),
            "changed": result.changed,
        }
    except Exception as exc:  # noqa: BLE001
        return {"applied": 0, "skipped": 0, "changed": False, "error": str(exc)}


def _print_table(rows: list[dict[str, Any]]) -> None:
    header = f"{'Hero':<12} {'adds':>5} {'updates':>7} {'weaker':>6} {'applied':>7} {'skipped':>7} {'changed':>7}"
    print(_DIVIDER)
    print(header)
    print(_DIVIDER)
    for row in rows:
        hero = row["hero"]
        s = row["summary"]
        a = row["apply"]
        error = a.get("error", "")
        changed = str(a.get("changed", "?"))
        if error:
            print(f"{hero:<12} {'ERR':>5} {error[:40]}")
        else:
            print(
                f"{hero:<12} {s['adds']:>5} {s['updates']:>7} {s['weaker']:>6}"
                f" {a['applied']:>7} {a['skipped']:>7} {changed:>7}"
            )
    print(_DIVIDER)


def _print_comparison_table(comparison: list[dict[str, Any]]) -> None:
    """Print side-by-side old-vs-new table for cross-source confidence promotion."""
    print(_DIVIDER)
    print("Cross-source confidence promotion: OLD (current) vs NEW (promote_cross_source)")
    print(_DIVIDER)
    header = (
        f"{'Hero':<12}"
        f" {'old_weaker':>10} {'new_weaker':>10} {'Δweaker':>8}"
        f" {'old_applied':>11} {'new_applied':>11} {'Δapplied':>9}"
    )
    print(header)
    print(_DIVIDER)
    for row in comparison:
        hero = row["hero"]
        old_s = row["old_summary"]
        new_s = row["new_summary"]
        old_a = row["old_apply"]
        new_a = row["new_apply"]
        if old_a.get("error") or new_a.get("error"):
            err = old_a.get("error") or new_a.get("error") or "?"
            print(f"{hero:<12} ERR {err[:50]}")
            continue
        old_weaker = old_s.get("weaker", 0)
        new_weaker = new_s.get("weaker", 0)
        old_applied = old_a.get("applied", 0)
        new_applied = new_a.get("applied", 0)
        delta_weaker = new_weaker - old_weaker
        delta_applied = new_applied - old_applied
        print(
            f"{hero:<12}"
            f" {old_weaker:>10} {new_weaker:>10} {delta_weaker:>+8}"
            f" {old_applied:>11} {new_applied:>11} {delta_applied:>+9}"
        )
    print(_DIVIDER)
    print("Δweaker < 0  = items rescued from weaker_signals into top_line (good)")
    print("Δapplied > 0 = more items reach the catalog applier (good)")


def _run_hero_pair(
    hero: str,
    *,
    state_file: Path,
    tracker_repo: Path,
    stats_dir: Path,
    old_output_dir: Path,
    new_output_dir: Path,
    no_bazaardb: bool,
) -> dict[str, Any]:
    """Run old and new rules for one hero; return comparison row."""
    old_diff = _run_hero(
        hero,
        state_file=state_file,
        tracker_repo=tracker_repo,
        stats_dir=stats_dir,
        output_dir=old_output_dir,
        no_bazaardb=no_bazaardb,
        promote_cross_source=False,
    )
    new_diff = _run_hero(
        hero,
        state_file=state_file,
        tracker_repo=tracker_repo,
        stats_dir=stats_dir,
        output_dir=new_output_dir,
        no_bazaardb=no_bazaardb,
        promote_cross_source=True,
    )
    return {
        "hero": hero,
        "old_summary": _summarize(old_diff),
        "new_summary": _summarize(new_diff),
        "old_apply": _apply_summary(old_diff, tracker_repo, hero),
        "new_apply": _apply_summary(new_diff, tracker_repo, hero),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--hero",
        nargs="*",
        metavar="HERO",
        default=HEROES,
        help="Heroes to sweep (default: all 7)",
    )
    parser.add_argument(
        "--tracker-repo",
        type=Path,
        default=_HERE.parent / "bazaar_coach",
        help="Path to local bazaar_coach checkout (default: ../bazaar_coach)",
    )
    parser.add_argument(
        "--stats-dir",
        type=Path,
        default=_HERE / "stats",
        help="Stats sidecar directory (read-only; default: ./stats)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_HERE / "artifacts" / "verify",
        help="Directory for diff/proposal output (default: ./artifacts/verify)",
    )
    parser.add_argument(
        "--no-bazaardb",
        action="store_true",
        help="Skip bazaardb fetch (useful for offline testing)",
    )
    parser.add_argument(
        "--compare-cross-source",
        action="store_true",
        default=False,
        help=(
            "Run each hero twice (old rules + new promote_cross_source rules) and "
            "print a side-by-side comparison.  This is the shadow comparison "
            "required before enabling --promote-cross-source in production.  "
            "Old diffs go to <output-dir>/old/, new diffs to <output-dir>/new/."
        ),
    )
    args = parser.parse_args(argv)

    heroes: list[str] = args.hero or HEROES
    tracker_repo: Path = args.tracker_repo
    stats_dir: Path = args.stats_dir
    output_dir: Path = args.output_dir

    if args.compare_cross_source:
        print(f"sweep_dry_run --compare-cross-source: {len(heroes)} hero(es)")
        print(f"  tracker: {tracker_repo}")
        print(f"  stats:   {stats_dir}  (read-only)")
        print(f"  NEVER touching pipeline_state.json")
        print(f"  old diffs → {output_dir / 'old'}")
        print(f"  new diffs → {output_dir / 'new'}")
        print()

        with tempfile.TemporaryDirectory(prefix="sweep_dry_run_") as tmp:
            state_file = _write_temp_state(tmp)
            print(f"  temp state file: {state_file}")
            print()

            comparison: list[dict[str, Any]] = []
            for hero in heroes:
                print(f"  running {hero} (old+new)...", end=" ", flush=True)
                try:
                    row = _run_hero_pair(
                        hero,
                        state_file=state_file,
                        tracker_repo=tracker_repo,
                        stats_dir=stats_dir,
                        old_output_dir=output_dir / "old",
                        new_output_dir=output_dir / "new",
                        no_bazaardb=args.no_bazaardb,
                    )
                    comparison.append(row)
                    print("ok")
                except Exception as exc:  # noqa: BLE001
                    comparison.append(
                        {
                            "hero": hero,
                            "old_summary": {},
                            "new_summary": {},
                            "old_apply": {"error": str(exc)},
                            "new_apply": {"error": str(exc)},
                        }
                    )
                    print(f"ERROR: {exc}")

        print()
        _print_comparison_table(comparison)
        return 0

    print(f"sweep_dry_run: {len(heroes)} hero(es), output → {output_dir}")
    print(f"  tracker: {tracker_repo}")
    print(f"  stats:   {stats_dir}  (read-only)")
    print(f"  NEVER touching pipeline_state.json")
    print()

    with tempfile.TemporaryDirectory(prefix="sweep_dry_run_") as tmp:
        state_file = _write_temp_state(tmp)
        print(f"  temp state file: {state_file}")
        print()

        rows: list[dict[str, Any]] = []
        for hero in heroes:
            print(f"  running {hero}...", end=" ", flush=True)
            try:
                diff = _run_hero(
                    hero,
                    state_file=state_file,
                    tracker_repo=tracker_repo,
                    stats_dir=stats_dir,
                    output_dir=output_dir,
                    no_bazaardb=args.no_bazaardb,
                )
                summary = _summarize(diff)
                apply = _apply_summary(diff, tracker_repo, hero)
                rows.append({"hero": hero, "summary": summary, "apply": apply})
                print("ok")
            except Exception as exc:  # noqa: BLE001
                rows.append(
                    {
                        "hero": hero,
                        "summary": {},
                        "apply": {"applied": 0, "skipped": 0, "changed": False, "error": str(exc)},
                    }
                )
                print(f"ERROR: {exc}")

    print()
    _print_table(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
