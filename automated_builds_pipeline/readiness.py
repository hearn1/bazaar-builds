"""Live-cron promotion readiness gate for the automated builds pipeline.

Evaluates whether the pipeline meets the criteria for live_cron promotion:
  - At least N=2 healthy bazaardb patch windows across all hero sidecars
  - Shadow output spans >=60 calendar days (oldest sidecar observed_at -> now)
  - Classifier/provider readiness (semantic_classification seen) or a waiver file
  - No malformed-shadow run in the last 14 days

"Healthy" bazaardb window predicate
-------------------------------------
A SourceWindowSummary from source_windows["bazaardb"] is healthy when:
  1. health_status == "healthy"  (explicit field written by append_window via WindowObservation)
  2. window_id does not end with ":skipped" or ":unknown"  (skipped/operator-skip entries)

The classification_mode/semantic_classification fields live only in diff artifacts, not in
the stats sidecar. Consequently the healthy-window count cannot introspect classifier
quality at the per-window level; that aspect is covered separately by the
classifier-readiness check.

Open question for maintainer: should healthy-window count be gated per-hero (require
>=2 healthy windows in every hero's sidecar) rather than globally across all heroes?
Currently the check counts distinct window_ids across all heroes, so a hero that ran
both windows while others ran 0 would still pass. Tightening this to per-hero is
more conservative and is recommended once a few shadow cycles complete.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from automated_builds_pipeline.state import load_state
from automated_builds_pipeline.stats import HeroStats, StatsError

MIN_HEALTHY_WINDOWS = 2
MIN_SHADOW_DAYS = 60
MALFORMED_LOOKBACK_DAYS = 14
BAZAARDB_SOURCE = "bazaardb"
_UNHEALTHY_STATUSES = frozenset({"unhealthy", "error", "skipped"})
_BAD_WINDOW_SUFFIXES = (":skipped", ":unknown")


@dataclass
class ReadinessReport:
    """Result of a readiness evaluation."""

    ready: bool
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "summary": self.summary,
        }


def evaluate_readiness(
    stats_dir: Path,
    state_path: Path,
    waiver_dir: Optional[Path] = None,
    *,
    now: Optional[datetime] = None,
) -> ReadinessReport:
    """Evaluate whether the pipeline is ready for live_cron promotion.

    Pure function — no side effects, safe to call in CI and tests.

    Args:
        stats_dir: Directory containing ``<hero>_stats.json`` sidecars.
        state_path: Path to ``pipeline_state.json``.
        waiver_dir: Optional directory checked for ``classifier_waiver_*.md`` files.
        now: Override current UTC time (for testing).

    Returns:
        A :class:`ReadinessReport` with ready, blockers, warnings, and summary.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    blockers: list[str] = []
    warnings: list[str] = []

    # --- Load all sidecars -------------------------------------------------------
    sidecars = _load_all_sidecars(stats_dir)

    # --- Check 1: healthy bazaardb patch windows ---------------------------------
    healthy_windows, oldest_observed_at = _collect_bazaardb_windows(sidecars)
    healthy_count = len(healthy_windows)

    if healthy_count < MIN_HEALTHY_WINDOWS:
        blockers.append(
            f"Insufficient healthy bazaardb patch windows: {healthy_count}/{MIN_HEALTHY_WINDOWS} required. "
            f"Shadow runs must accumulate at least {MIN_HEALTHY_WINDOWS} distinct healthy bazaardb windows across all hero sidecars."
        )

    # --- Check 2: shadow output spans >=60 calendar days ------------------------
    shadow_days: Optional[float] = None
    if oldest_observed_at is not None:
        delta = now - oldest_observed_at
        shadow_days = delta.total_seconds() / 86400.0
        if shadow_days < MIN_SHADOW_DAYS:
            blockers.append(
                f"Shadow output spans only {shadow_days:.1f} days; {MIN_SHADOW_DAYS} required. "
                f"Oldest healthy bazaardb window observed at {oldest_observed_at.isoformat()}."
            )
    else:
        # No healthy windows at all — covered by check 1, but add a shadow_days note
        shadow_days = 0.0
        if healthy_count >= MIN_HEALTHY_WINDOWS:
            # This shouldn't happen (healthy windows require a valid observed_at),
            # but be defensive.
            warnings.append("Could not determine oldest shadow timestamp despite healthy windows.")

    # --- Check 3: classifier/provider readiness or explicit waiver ---------------
    classifier_ready, waiver_found = _check_classifier_readiness(sidecars, waiver_dir)
    if not classifier_ready and not waiver_found:
        blockers.append(
            "Semantic classifier not ready and no classifier waiver found. "
            "Either confirm semantic_classification=true in a recent diff sidecar, "
            "or place a waivers/classifier_waiver_*.md file to record the explicit decision."
        )

    # --- Check 4: no malformed-shadow run in last 14 days ------------------------
    recent_cutoff = now - timedelta(days=MALFORMED_LOOKBACK_DAYS)
    malformed_recent = _find_malformed_recent(sidecars, recent_cutoff)
    if malformed_recent:
        blockers.append(
            f"Malformed/unhealthy bazaardb shadow runs found in the last {MALFORMED_LOOKBACK_DAYS} days: "
            + ", ".join(sorted(malformed_recent))
            + ". Investigate before promoting."
        )

    # --- Current pipeline phase --------------------------------------------------
    current_phase = _read_current_phase(state_path)

    summary: dict[str, Any] = {
        "phase": current_phase,
        "healthy_bazaardb_windows": healthy_count,
        "shadow_days": round(shadow_days, 1) if shadow_days is not None else None,
        "classifier_ready": classifier_ready,
        "waiver_found": waiver_found,
        "malformed_recent_count": len(malformed_recent),
    }

    return ReadinessReport(
        ready=len(blockers) == 0,
        blockers=blockers,
        warnings=warnings,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_all_sidecars(stats_dir: Path) -> list[HeroStats]:
    """Load all parseable hero stats sidecars from *stats_dir*."""
    sidecars: list[HeroStats] = []
    if not stats_dir.exists():
        return sidecars
    for path in sorted(stats_dir.glob("*_stats.json")):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            hero = data.get("hero")
            if not isinstance(hero, str) or not hero:
                continue
            sidecars.append(HeroStats.from_dict(data))
        except (StatsError, KeyError, ValueError):
            # Malformed sidecar — skip; can't extract windows from it
            continue
    return sidecars


def _is_healthy_bazaardb_window_id(window_id: str) -> bool:
    """Return True if *window_id* is a non-skipped/non-unknown bazaardb window."""
    for suffix in _BAD_WINDOW_SUFFIXES:
        if window_id.endswith(suffix):
            return False
    return True


def _collect_bazaardb_windows(
    sidecars: list[HeroStats],
) -> tuple[set[str], Optional[datetime]]:
    """Return (set of distinct healthy window_ids, oldest observed_at datetime)."""
    seen_ids: set[str] = set()
    oldest: Optional[datetime] = None

    for sidecar in sidecars:
        for summary in sidecar.source_windows.get(BAZAARDB_SOURCE, []):
            if summary.health_status in _UNHEALTHY_STATUSES:
                continue
            if not _is_healthy_bazaardb_window_id(summary.window_id):
                continue
            seen_ids.add(summary.window_id)
            try:
                ts = _parse_iso(summary.observed_at)
            except ValueError:
                continue
            if oldest is None or ts < oldest:
                oldest = ts

    return seen_ids, oldest


def _find_malformed_recent(
    sidecars: list[HeroStats],
    cutoff: datetime,
) -> list[str]:
    """Return list of unhealthy bazaardb window_ids observed after *cutoff*."""
    bad: list[str] = []
    for sidecar in sidecars:
        for summary in sidecar.source_windows.get(BAZAARDB_SOURCE, []):
            if summary.health_status not in _UNHEALTHY_STATUSES:
                continue
            try:
                ts = _parse_iso(summary.observed_at)
            except ValueError:
                continue
            if ts >= cutoff:
                bad.append(f"{sidecar.hero}:{summary.window_id}")
    return bad


def _check_classifier_readiness(
    sidecars: list[HeroStats],
    waiver_dir: Optional[Path],
) -> tuple[bool, bool]:
    """Return (classifier_ready, waiver_found).

    classifier_ready is True if any sidecar's source_windows metadata hints at
    semantic classification having been used.  Because the stats sidecar schema
    (v1) does not store a top-level semantic_classification flag, we rely on the
    waiver path as the primary signal in shadow_cron, and treat classifier_ready
    as False until a future sidecar schema version adds it.

    waiver_found is True if *waiver_dir* contains at least one
    ``classifier_waiver_*.md`` file.
    """
    # Stats sidecar v1 has no top-level semantic_classification flag.
    # classifier_ready remains False until the sidecar schema exposes it.
    classifier_ready = False

    waiver_found = False
    if waiver_dir is not None and waiver_dir.is_dir():
        waiver_found = any(waiver_dir.glob("classifier_waiver_*.md"))

    return classifier_ready, waiver_found


def _read_current_phase(state_path: Path) -> Optional[str]:
    try:
        state = load_state(state_path)
        return state.phase
    except Exception:
        return None


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; attach UTC if naive."""
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check live_cron promotion readiness.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Default: human-readable checklist.\n"
            "--json : emit ReadinessReport as JSON.\n"
            "--enforce : exit 0 if ready, exit 1 if not (for CI gates).\n"
        ),
    )
    parser.add_argument(
        "--stats-dir",
        type=Path,
        default=Path("stats"),
        help="Directory containing *_stats.json sidecars (default: stats/)",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path("pipeline_state.json"),
        help="pipeline_state.json path (default: pipeline_state.json)",
    )
    parser.add_argument(
        "--waiver-dir",
        type=Path,
        default=None,
        help="Directory to check for classifier_waiver_*.md files",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
        help="Emit ReadinessReport as JSON",
    )
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="Exit 1 if not ready (CI gate mode)",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    report = evaluate_readiness(
        stats_dir=args.stats_dir,
        state_path=args.state_file,
        waiver_dir=args.waiver_dir,
    )

    if args.emit_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_human(report)

    if args.enforce:
        return 0 if report.ready else 1
    return 0


def _print_human(report: ReadinessReport) -> None:
    status = "READY" if report.ready else "NOT READY"
    print(f"live_cron readiness: {status}")
    print()

    checks = [
        (
            f"Healthy bazaardb windows: {report.summary.get('healthy_bazaardb_windows', 0)}/{MIN_HEALTHY_WINDOWS}",
            report.summary.get("healthy_bazaardb_windows", 0) >= MIN_HEALTHY_WINDOWS,
        ),
        (
            f"Shadow output span: {report.summary.get('shadow_days', 0):.1f}/{MIN_SHADOW_DAYS} days",
            (report.summary.get("shadow_days") or 0) >= MIN_SHADOW_DAYS,
        ),
        (
            f"Classifier ready: {report.summary.get('classifier_ready')} | "
            f"Waiver found: {report.summary.get('waiver_found')}",
            report.summary.get("classifier_ready") or report.summary.get("waiver_found"),
        ),
        (
            f"Recent malformed runs (last {MALFORMED_LOOKBACK_DAYS}d): "
            f"{report.summary.get('malformed_recent_count', 0)}",
            report.summary.get("malformed_recent_count", 0) == 0,
        ),
    ]
    for label, ok in checks:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {label}")

    if report.blockers:
        print()
        print("Blockers:")
        for b in report.blockers:
            print(f"  - {b}")
    if report.warnings:
        print()
        print("Warnings:")
        for w in report.warnings:
            print(f"  - {w}")
    print()
    s = report.summary
    print(
        f"Summary: phase={s.get('phase')} windows={s.get('healthy_bazaardb_windows')} "
        f"shadow_days={s.get('shadow_days')} classifier_ready={s.get('classifier_ready')} "
        f"waiver={s.get('waiver_found')}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
