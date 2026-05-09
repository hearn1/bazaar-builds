"""Tests for automated_builds_pipeline.readiness."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from automated_builds_pipeline.readiness import (
    MIN_HEALTHY_WINDOWS,
    MIN_SHADOW_DAYS,
    ReadinessReport,
    evaluate_readiness,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _make_state(tmp_path: Path, phase: str = "shadow_cron") -> Path:
    state_path = tmp_path / "pipeline_state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": phase,
                "dry_run": True,
                "patch_label": None,
                "expected_bazaardb_patch_label": None,
                "freeze_removals_until": None,
                "hero_freezes": {},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return state_path


def _make_sidecar(
    stats_dir: Path,
    hero: str,
    windows: list[dict],
) -> None:
    """Write a minimal stats sidecar with the given bazaardb source_windows."""
    sidecar = {
        "schema_version": 1,
        "hero": hero,
        "generated_at": _iso(NOW),
        "retention_windows": {
            "bazaardb": None,
            "bazaar_builds_net": 26,
            "mobalytics_build_articles": 26,
            "mobalytics_meta_builds": 26,
        },
        "source_windows": {
            "bazaardb": windows,
        },
        "items": {},
    }
    slug = hero.lower()
    (stats_dir / f"{slug}_stats.json").write_text(
        json.dumps(sidecar, indent=2) + "\n", encoding="utf-8"
    )


def _healthy_window(window_id: str, observed_at: str) -> dict:
    return {
        "window_id": window_id,
        "observed_at": observed_at,
        "health_status": "healthy",
        "details": [],
    }


def _unhealthy_window(window_id: str, observed_at: str) -> dict:
    return {
        "window_id": window_id,
        "observed_at": observed_at,
        "health_status": "error",
        "details": ["fetch failed"],
    }


# ---------------------------------------------------------------------------
# Case 1: Empty stats dir → not ready, blocker about windows count
# ---------------------------------------------------------------------------


def test_empty_stats_dir_not_ready(tmp_path):
    stats_dir = tmp_path / "stats"
    stats_dir.mkdir()
    state_path = _make_state(tmp_path)

    report = evaluate_readiness(stats_dir, state_path, now=NOW)

    assert isinstance(report, ReadinessReport)
    assert report.ready is False
    assert any("bazaardb patch windows" in b for b in report.blockers), report.blockers
    assert report.summary["healthy_bazaardb_windows"] == 0


# ---------------------------------------------------------------------------
# Case 2: 3 sidecars but spanning only 5 days → not ready, shadow_days blocker
# ---------------------------------------------------------------------------


def test_three_sidecars_short_span_not_ready(tmp_path):
    stats_dir = tmp_path / "stats"
    stats_dir.mkdir()
    state_path = _make_state(tmp_path)

    # All three windows clustered in last 5 days
    five_days_ago = NOW - timedelta(days=5)
    four_days_ago = NOW - timedelta(days=4)
    three_days_ago = NOW - timedelta(days=3)

    _make_sidecar(stats_dir, "karnok", [_healthy_window("bazaardb:w1", _iso(five_days_ago))])
    _make_sidecar(stats_dir, "mak", [_healthy_window("bazaardb:w2", _iso(four_days_ago))])
    _make_sidecar(stats_dir, "dooley", [_healthy_window("bazaardb:w3", _iso(three_days_ago))])

    report = evaluate_readiness(stats_dir, state_path, now=NOW)

    assert report.ready is False
    shadow_blocker = [b for b in report.blockers if "days" in b]
    assert shadow_blocker, f"Expected shadow_days blocker, got blockers: {report.blockers}"
    assert report.summary["healthy_bazaardb_windows"] == 3
    assert report.summary["shadow_days"] < MIN_SHADOW_DAYS


# ---------------------------------------------------------------------------
# Case 3: 3 healthy sidecars over 60+ days but no classifier and no waiver → not ready
# ---------------------------------------------------------------------------


def test_healthy_windows_over_60_days_no_classifier_not_ready(tmp_path):
    stats_dir = tmp_path / "stats"
    stats_dir.mkdir()
    state_path = _make_state(tmp_path)

    sixty_five_days_ago = NOW - timedelta(days=65)
    forty_days_ago = NOW - timedelta(days=40)
    ten_days_ago = NOW - timedelta(days=10)

    _make_sidecar(stats_dir, "karnok", [_healthy_window("bazaardb:w1", _iso(sixty_five_days_ago))])
    _make_sidecar(stats_dir, "mak", [_healthy_window("bazaardb:w2", _iso(forty_days_ago))])
    _make_sidecar(stats_dir, "dooley", [_healthy_window("bazaardb:w3", _iso(ten_days_ago))])

    # No waiver_dir provided → no waiver
    report = evaluate_readiness(stats_dir, state_path, waiver_dir=None, now=NOW)

    assert report.ready is False
    classifier_blocker = [b for b in report.blockers if "classifier" in b.lower() or "waiver" in b.lower()]
    assert classifier_blocker, f"Expected classifier blocker, got blockers: {report.blockers}"
    # Window and shadow checks should pass
    assert report.summary["healthy_bazaardb_windows"] >= MIN_HEALTHY_WINDOWS
    assert report.summary["shadow_days"] >= MIN_SHADOW_DAYS


# ---------------------------------------------------------------------------
# Case 4: 3 healthy sidecars + waiver file → ready=True
# ---------------------------------------------------------------------------


def test_healthy_windows_with_waiver_is_ready(tmp_path):
    stats_dir = tmp_path / "stats"
    stats_dir.mkdir()
    state_path = _make_state(tmp_path)

    waiver_dir = tmp_path / "waivers"
    waiver_dir.mkdir()
    (waiver_dir / "classifier_waiver_2026-05-01.md").write_text(
        "Operator waiver: proceeding without semantic classifier.\n",
        encoding="utf-8",
    )

    sixty_five_days_ago = NOW - timedelta(days=65)
    forty_days_ago = NOW - timedelta(days=40)
    ten_days_ago = NOW - timedelta(days=10)

    _make_sidecar(stats_dir, "karnok", [_healthy_window("bazaardb:w1", _iso(sixty_five_days_ago))])
    _make_sidecar(stats_dir, "mak", [_healthy_window("bazaardb:w2", _iso(forty_days_ago))])
    _make_sidecar(stats_dir, "dooley", [_healthy_window("bazaardb:w3", _iso(ten_days_ago))])

    report = evaluate_readiness(stats_dir, state_path, waiver_dir=waiver_dir, now=NOW)

    assert report.ready is True, f"Expected ready but got blockers: {report.blockers}"
    assert report.blockers == []
    assert report.summary["waiver_found"] is True
    assert report.summary["healthy_bazaardb_windows"] == 3
    assert report.summary["shadow_days"] >= MIN_SHADOW_DAYS


# ---------------------------------------------------------------------------
# Extra: malformed/unhealthy recent window blocks promotion even if window count passes
# ---------------------------------------------------------------------------


def test_recent_malformed_window_blocks_promotion(tmp_path):
    stats_dir = tmp_path / "stats"
    stats_dir.mkdir()
    state_path = _make_state(tmp_path)

    waiver_dir = tmp_path / "waivers"
    waiver_dir.mkdir()
    (waiver_dir / "classifier_waiver_2026-05-01.md").write_text(
        "Waiver.\n", encoding="utf-8"
    )

    sixty_five_days_ago = NOW - timedelta(days=65)
    forty_days_ago = NOW - timedelta(days=40)
    ten_days_ago = NOW - timedelta(days=10)
    two_days_ago = NOW - timedelta(days=2)

    _make_sidecar(
        stats_dir,
        "karnok",
        [
            _healthy_window("bazaardb:w1", _iso(sixty_five_days_ago)),
            _healthy_window("bazaardb:w2", _iso(forty_days_ago)),
            _healthy_window("bazaardb:w3", _iso(ten_days_ago)),
            _unhealthy_window("bazaardb:w4", _iso(two_days_ago)),  # recent bad run
        ],
    )

    report = evaluate_readiness(stats_dir, state_path, waiver_dir=waiver_dir, now=NOW)

    assert report.ready is False
    malformed_blocker = [b for b in report.blockers if "malformed" in b.lower() or "unhealthy" in b.lower()]
    assert malformed_blocker, f"Expected malformed blocker, got: {report.blockers}"
    assert report.summary["malformed_recent_count"] >= 1


# ---------------------------------------------------------------------------
# Extra: skipped window_ids are not counted as healthy
# ---------------------------------------------------------------------------


def test_skipped_window_ids_not_counted(tmp_path):
    stats_dir = tmp_path / "stats"
    stats_dir.mkdir()
    state_path = _make_state(tmp_path)

    sixty_five_days_ago = NOW - timedelta(days=65)
    _make_sidecar(
        stats_dir,
        "karnok",
        [
            # These have healthy status but :skipped window_id — should not count
            _healthy_window("bazaardb:skipped", _iso(sixty_five_days_ago)),
            _healthy_window("bazaardb:unknown", _iso(sixty_five_days_ago)),
        ],
    )

    report = evaluate_readiness(stats_dir, state_path, now=NOW)

    assert report.ready is False
    assert report.summary["healthy_bazaardb_windows"] == 0


# ---------------------------------------------------------------------------
# Extra: report is JSON-serializable
# ---------------------------------------------------------------------------


def test_report_is_json_serializable(tmp_path):
    stats_dir = tmp_path / "stats"
    stats_dir.mkdir()
    state_path = _make_state(tmp_path)

    report = evaluate_readiness(stats_dir, state_path, now=NOW)
    payload = json.dumps(report.to_dict())
    loaded = json.loads(payload)
    assert loaded["ready"] is False
