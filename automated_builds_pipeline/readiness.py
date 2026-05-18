"""Live-cron promotion readiness gate for the automated builds pipeline.

Evaluates whether the pipeline meets the criteria for live_cron promotion:
  - At least N=2 healthy bazaardb patch windows across all hero sidecars
  - Shadow output spans >=14 calendar days (oldest sidecar observed_at -> now)
  - Deterministic classifier readiness or a classifier waiver file
  - No malformed-shadow run in the last 14 days

Gate 1 and Gate 2 can be bypassed only by an explicit operator promotion
waiver file named ``live_cron_promotion_waiver_*.md``. The waiver is reported
in the readiness summary and warnings so promotion is auditable rather than a
silent threshold change. The promotion waiver does not bypass classifier
readiness or recent malformed/unhealthy source runs.

"Healthy" bazaardb window predicate
-------------------------------------
A SourceWindowSummary from source_windows["bazaardb"] is healthy when:
  1. health_status == "healthy"  (explicit field written by append_window via WindowObservation)
  2. window_id does not end with ":skipped", ":unknown", or ":nopatch".
     ":nopatch" tags a run whose patch label fell back to a bare "Mon DD"
     string (no patch identity); such windows change every run and would
     inflate the distinct-patch-window count, so they are excluded here even
     though the run itself is health_status == "healthy" and evidence-bearing.

The classification_mode/semantic_classification fields live only in diff artifacts, not in
the stats sidecar. Consequently the healthy-window count cannot introspect classifier
quality at the per-window level; that aspect is covered separately by the
classifier-readiness check.

Gate 1 is counted per-hero: it passes when at least one hero's sidecar has >=2
distinct healthy patch windows. The count is not summed across heroes, so two
heroes with one window each does not satisfy the gate. summary
["healthy_windows_by_hero"] exposes the per-hero counts for transparency.
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
MIN_SHADOW_DAYS = 14
MIN_CLASSIFIED_DAYS = 14
MALFORMED_LOOKBACK_DAYS = 14
BAZAARDB_SOURCE = "bazaardb"
# Single shared definition; also imported by pipeline.py.
REAL_CLASSIFIER_MODES = frozenset({"deterministic"})
CLASSIFIER_WAIVER_PATTERN = "classifier_waiver_*.md"
PROMOTION_WAIVER_PATTERN = "live_cron_promotion_waiver_*.md"
_UNHEALTHY_STATUSES = frozenset({"unhealthy", "error", "skipped"})
_BAD_WINDOW_SUFFIXES = (":skipped", ":unknown", ":nopatch")


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
    promotion_waiver_found = _check_promotion_waiver(waiver_dir)
    waived_gates: list[str] = []

    # --- Load all sidecars -------------------------------------------------------
    sidecars = _load_all_sidecars(stats_dir)

    # --- Check 1: healthy bazaardb patch windows ---------------------------------
    # Counted per-hero: the gate passes when at least one hero's sidecar has
    # >=2 distinct healthy patch windows. The count is NOT summed across heroes,
    # so two heroes with one window each does not satisfy the gate.
    windows_by_hero, oldest_observed_at = _collect_bazaardb_windows(sidecars)
    windows_by_hero_counts = {hero: len(ids) for hero, ids in windows_by_hero.items()}
    healthy_count = max(windows_by_hero_counts.values(), default=0)

    if healthy_count < MIN_HEALTHY_WINDOWS:
        if promotion_waiver_found:
            waived_gates.append("gate1_bazaardb_patch_windows")
            warnings.append(
                f"Gate 1 waived by {PROMOTION_WAIVER_PATTERN}: best hero has "
                f"{healthy_count}/{MIN_HEALTHY_WINDOWS} healthy bazaardb patch windows."
            )
        else:
            blockers.append(
                f"Insufficient healthy bazaardb patch windows: best hero has "
                f"{healthy_count}/{MIN_HEALTHY_WINDOWS} required. No single hero sidecar "
                f"has at least {MIN_HEALTHY_WINDOWS} distinct healthy bazaardb patch windows."
            )

    # --- Classifier readiness (Gate 3) — computed first; Gate 2 depends on it ---
    classifier_ready, waiver_found = _check_classifier_readiness(sidecars, waiver_dir)
    classifier_started_at = _min_classifier_started_at(sidecars)

    # --- Check 2: classified-output / shadow-output span ------------------------
    shadow_days: Optional[float] = None
    if oldest_observed_at is not None:
        shadow_days = (now - oldest_observed_at).total_seconds() / 86400.0
    else:
        # No healthy windows at all — covered by check 1, but record a span note.
        shadow_days = 0.0
        if healthy_count >= MIN_HEALTHY_WINDOWS:
            # This shouldn't happen (healthy windows require a valid observed_at),
            # but be defensive.
            warnings.append("Could not determine oldest shadow timestamp despite healthy windows.")

    classified_days: Optional[float] = None
    if classifier_ready and classifier_started_at is not None:
        # A real classifier has run: only MIN_CLASSIFIED_DAYS of *classifier-produced*
        # output is required, measured from the durable classifier_started_at signal.
        effective_min_shadow_days = MIN_CLASSIFIED_DAYS
        classified_days = (now - classifier_started_at).total_seconds() / 86400.0
        if classified_days < MIN_CLASSIFIED_DAYS:
            if promotion_waiver_found:
                waived_gates.append("gate2_classified_output_span")
                warnings.append(
                    f"Gate 2 waived by {PROMOTION_WAIVER_PATTERN}: classifier-produced "
                    f"output spans {classified_days:.1f}/{MIN_CLASSIFIED_DAYS} days. "
                    f"First classified run at {classifier_started_at.isoformat()}."
                )
            else:
                blockers.append(
                    f"Classifier-produced output spans only {classified_days:.1f} days; "
                    f"{MIN_CLASSIFIED_DAYS} required. First classified run at "
                    f"{classifier_started_at.isoformat()}."
                )
    else:
        # No real classifier (or a waiver only) — the full 14-day shadow
        # requirement still applies. A waiver does NOT shorten this gate.
        effective_min_shadow_days = MIN_SHADOW_DAYS
        if shadow_days < MIN_SHADOW_DAYS:
            if promotion_waiver_found:
                waived_gates.append("gate2_shadow_output_span")
                warnings.append(
                    f"Gate 2 waived by {PROMOTION_WAIVER_PATTERN}: shadow output spans "
                    f"{shadow_days:.1f}/{MIN_SHADOW_DAYS} days with no classifier deployed."
                )
            else:
                blockers.append(
                    f"Shadow output spans only {shadow_days:.1f} days; {MIN_SHADOW_DAYS} required "
                    f"(no classifier deployed)."
                )

    # --- Check 3: deterministic classifier readiness or explicit waiver ----------
    if not classifier_ready and not waiver_found:
        blockers.append(
            "Semantic classifier not ready and no classifier waiver found. "
            "Either confirm a real classifier ran (last_classifier_mode in a hero "
            "sidecar), or place a waivers/classifier_waiver_*.md file to record the "
            "explicit decision."
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
        "healthy_windows_by_hero": dict(sorted(windows_by_hero_counts.items())),
        "shadow_days": round(shadow_days, 1) if shadow_days is not None else None,
        "effective_min_shadow_days": effective_min_shadow_days,
        "classified_days": round(classified_days, 1) if classified_days is not None else None,
        "classifier_started_at": (
            classifier_started_at.isoformat() if classifier_started_at is not None else None
        ),
        "classifier_ready": classifier_ready,
        "waiver_found": waiver_found,
        "promotion_waiver_found": promotion_waiver_found,
        "waived_gates": waived_gates,
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
) -> tuple[dict[str, set[str]], Optional[datetime]]:
    """Return (per-hero distinct healthy window_ids, oldest observed_at datetime).

    Gate 1 is counted per-hero (see :func:`evaluate_readiness`), so the windows
    are kept partitioned by hero rather than unioned. ``oldest`` is still the
    oldest healthy observed_at across *all* heroes — Gate 2's calendar span is
    a global measure and is unaffected by the per-hero Gate 1 change.
    """
    by_hero: dict[str, set[str]] = {}
    oldest: Optional[datetime] = None

    for sidecar in sidecars:
        for summary in sidecar.source_windows.get(BAZAARDB_SOURCE, []):
            if summary.health_status in _UNHEALTHY_STATUSES:
                continue
            if not _is_healthy_bazaardb_window_id(summary.window_id):
                continue
            by_hero.setdefault(sidecar.hero, set()).add(summary.window_id)
            try:
                ts = _parse_iso(summary.observed_at)
            except ValueError:
                continue
            if oldest is None or ts < oldest:
                oldest = ts

    return by_hero, oldest


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

    classifier_ready is True if any hero sidecar's most recent run used a real
    classifier (``last_classifier_mode`` in :data:`REAL_CLASSIFIER_MODES`). A
    later ``no_llm_shadow`` run flips ``last_classifier_mode`` back, correctly
    re-blocking Gate 3.

    waiver_found is True if *waiver_dir* contains at least one
    ``classifier_waiver_*.md`` file.
    """
    classifier_ready = any(
        s.last_classifier_mode in REAL_CLASSIFIER_MODES for s in sidecars
    )

    waiver_found = False
    if waiver_dir is not None and waiver_dir.is_dir():
        waiver_found = any(waiver_dir.glob(CLASSIFIER_WAIVER_PATTERN))

    return classifier_ready, waiver_found


def _check_promotion_waiver(waiver_dir: Optional[Path]) -> bool:
    """Return True when an explicit Gate 1/Gate 2 promotion waiver exists."""
    if waiver_dir is None or not waiver_dir.is_dir():
        return False
    return any(waiver_dir.glob(PROMOTION_WAIVER_PATTERN))


def _min_classifier_started_at(sidecars: list[HeroStats]) -> Optional[datetime]:
    """Return the earliest ``classifier_started_at`` across sidecars, or None.

    This is the durable classified-span anchor: it is written once on the first
    real-classifier run and never overwritten, so it survives a later regression
    to ``no_llm_shadow``.
    """
    oldest: Optional[datetime] = None
    for sidecar in sidecars:
        raw = sidecar.classifier_started_at
        if not raw:
            continue
        try:
            ts = _parse_iso(raw)
        except ValueError:
            continue
        if oldest is None or ts < oldest:
            oldest = ts
    return oldest


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
            report.summary.get("healthy_bazaardb_windows", 0) >= MIN_HEALTHY_WINDOWS
            or "gate1_bazaardb_patch_windows" in report.summary.get("waived_gates", []),
        ),
        _span_check(report.summary),
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
        mark = "OK" if ok else "FAIL"
        print(f"  {mark} {label}")
    print(f"  INFO Promotion waiver found: {report.summary.get('promotion_waiver_found')}")

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
        f"shadow_days={s.get('shadow_days')} classified_days={s.get('classified_days')} "
        f"effective_min_days={s.get('effective_min_shadow_days')} "
        f"classifier_started_at={s.get('classifier_started_at')} "
        f"classifier_ready={s.get('classifier_ready')} waiver={s.get('waiver_found')} "
        f"promotion_waiver={s.get('promotion_waiver_found')} "
        f"waived_gates={s.get('waived_gates')}"
    )


def _span_check(summary: dict[str, Any]) -> tuple[str, bool]:
    """Render the Gate-2 span check line using the effective threshold."""
    effective = summary.get("effective_min_shadow_days", MIN_SHADOW_DAYS)
    classified = summary.get("classified_days")
    if classified is not None:
        waived = "gate2_classified_output_span" in summary.get("waived_gates", [])
        return (
            f"Classified output span: {classified:.1f}/{effective} days "
            f"(classifier active)",
            classified >= effective or waived,
        )
    shadow = summary.get("shadow_days") or 0
    waived = "gate2_shadow_output_span" in summary.get("waived_gates", [])
    return (
        f"Shadow output span: {shadow:.1f}/{effective} days (no classifier)",
        shadow >= effective or waived,
    )


if __name__ == "__main__":
    raise SystemExit(main())
