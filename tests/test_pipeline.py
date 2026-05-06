import json
from pathlib import Path

import pytest

from automated_builds_pipeline import pipeline
from automated_builds_pipeline.evaluator import EvaluationResult
from automated_builds_pipeline.sources.base import SourceFetchResult
from automated_builds_pipeline.stats import (
    ItemWindowEvidence,
    WindowObservation,
    append_window,
    load_stats,
    save_stats,
    stats_path,
)


OBSERVED_AT = "2026-05-05T12:00:00Z"


def source_result(source: str, *, status: str = "healthy", details: list[str] | None = None) -> SourceFetchResult:
    return SourceFetchResult(
        observation=WindowObservation(
            window_id=f"{source}:window",
            observed_at=OBSERVED_AT,
            items=[ItemWindowEvidence("Pufferfish")] if status == "healthy" else [],
        ),
        status=status,
        details=details or [],
    )


def write_state(path: Path, phase: str, **overrides) -> None:
    payload = {
        "schema_version": 1,
        "phase": phase,
        "dry_run": phase != "live_cron",
        "patch_label": None,
        "expected_bazaardb_patch_label": None,
        "freeze_removals_until": None,
        "hero_freezes": {},
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


def make_tracker(tmp_path: Path) -> Path:
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    (tracker / "card_cache_names.txt").write_text("Pufferfish\n", encoding="utf-8")
    (tracker / "karnok_builds.json").write_text(json.dumps({"items": []}), encoding="utf-8")
    return tracker


def patch_fetchers(monkeypatch, *, bazaardb_results: list[SourceFetchResult] | None = None):
    results = list(bazaardb_results or [source_result("bazaardb")])

    def fetch_bazaardb(hero, options):
        return results.pop(0)

    monkeypatch.setattr(pipeline.bazaardb, "fetch_meta", fetch_bazaardb)
    monkeypatch.setattr(
        pipeline.mobalytics,
        "fetch_meta_builds",
        lambda hero, options: source_result("mobalytics_meta_builds"),
    )
    monkeypatch.setattr(
        pipeline.mobalytics,
        "fetch_build_articles",
        lambda slugs, options: source_result("mobalytics_build_articles", status="skipped", details=["no slugs"]),
    )
    monkeypatch.setattr(
        pipeline.bazaar_builds_net,
        "fetch_builds",
        lambda hero, category_url, options: source_result("bazaar_builds_net"),
    )


def patch_evaluator(monkeypatch, captured):
    def fake_evaluate(hero, catalog_items, stats, source_results, state):
        captured["source_results"] = list(source_results)
        captured["state"] = state
        return EvaluationResult(
            hero=hero,
            freeze_active=state.freeze_status(hero).active,
            generated_at=OBSERVED_AT,
            run_id="run1",
            source_health=[],
            rows=[],
        )

    monkeypatch.setattr(pipeline, "evaluate_hero", fake_evaluate)


def patch_diff(monkeypatch, diff_json: dict | None = None):
    payload = diff_json or {
        "schema_version": 1,
        "hero": "Karnok",
        "window_id": "w1",
        "source_window": {},
        "freeze_state": {"removals_frozen": False, "patch_label": None},
        "source_health": [],
        "proposed_changes": {
            "archetype_updates": [{"phase": "mid", "archetype": "Axe", "missing_items": []}],
            "archetype_additions": [],
            "archetype_removal_candidates": [],
            "item_removal_candidates": [],
            "archetype_reshuffles": [],
        },
        "weaker_signals": [],
        "noise": [],
    }
    monkeypatch.setattr(pipeline.diff, "generate_diff", lambda *args, **kwargs: payload)


@pytest.mark.parametrize(
    ("phase", "expected_pr", "expected_artifacts"),
    [
        ("implementation", False, False),
        ("local_dry_run", False, True),
        ("shadow_cron", False, True),
        ("live_cron", True, True),
    ],
)
def test_phase_gates(monkeypatch, tmp_path, phase, expected_pr, expected_artifacts):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, phase)
    tracker = make_tracker(tmp_path)
    captured = {}
    patch_fetchers(monkeypatch)
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch)
    pr_calls = []

    result = pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=tmp_path / "stats",
        output_dir=tmp_path / "artifacts",
        pr_action=lambda *args: pr_calls.append(args),
    )

    assert result.pr_invoked is expected_pr
    assert bool(pr_calls) is expected_pr
    assert (tmp_path / "artifacts" / "Karnok_diff.json").exists() is expected_artifacts


def test_bazaardb_retry_succeeds_after_two_transient_failures(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "local_dry_run")
    tracker = make_tracker(tmp_path)
    calls = []

    def fetch_bazaardb(hero, options):
        calls.append(1)
        if len(calls) < 3:
            return source_result("bazaardb", status="unhealthy", details=["cloudflare timeout"])
        return source_result("bazaardb")

    patch_fetchers(monkeypatch)
    monkeypatch.setattr(pipeline.bazaardb, "fetch_meta", fetch_bazaardb)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch)

    pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=tmp_path / "stats",
        output_dir=tmp_path / "artifacts",
    )

    assert len(calls) == 3
    bazaardb_result = next(row for row in captured["source_results"] if row.observation.window_id.startswith("bazaardb:"))
    assert bazaardb_result.status == "healthy"


def test_bazaardb_retry_exhausted_keeps_unhealthy_result(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "local_dry_run")
    tracker = make_tracker(tmp_path)
    calls = []

    def fetch_bazaardb(hero, options):
        calls.append(1)
        return source_result("bazaardb", status="unhealthy", details=["cloudflare timeout"])

    patch_fetchers(monkeypatch)
    monkeypatch.setattr(pipeline.bazaardb, "fetch_meta", fetch_bazaardb)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch)

    pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=tmp_path / "stats",
        output_dir=tmp_path / "artifacts",
    )

    assert len(calls) == 3
    bazaardb_result = next(row for row in captured["source_results"] if row.observation.window_id.startswith("bazaardb:"))
    assert bazaardb_result.status == "unhealthy"


def test_no_bazaardb_marks_skipped_without_fetch(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "local_dry_run")
    tracker = make_tracker(tmp_path)
    patch_fetchers(monkeypatch)
    monkeypatch.setattr(pipeline.bazaardb, "fetch_meta", lambda hero, options: pytest.fail("should not fetch bazaardb"))
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch)

    pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=tmp_path / "stats",
        output_dir=tmp_path / "artifacts",
        no_bazaardb=True,
    )

    bazaardb_result = next(row for row in captured["source_results"] if row.observation.window_id.startswith("bazaardb:"))
    assert bazaardb_result.status == "skipped"
    assert bazaardb_result.details == ["operator skip"]


def test_sidecar_not_touched_when_evaluator_raises(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "local_dry_run")
    tracker = make_tracker(tmp_path)
    stats_dir = tmp_path / "stats"
    patch_fetchers(monkeypatch)
    monkeypatch.setattr(pipeline, "evaluate_hero", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        pipeline.run(
            hero="Karnok",
            state_file=state_file,
            tracker_repo=tracker,
            stats_dir=stats_dir,
            output_dir=tmp_path / "artifacts",
        )

    assert not stats_path("Karnok", stats_dir).exists()


def test_run_merges_existing_stats_with_current_fetch_output(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "local_dry_run")
    tracker = make_tracker(tmp_path)
    stats_dir = tmp_path / "stats"
    prior_stats = load_stats("Karnok", stats_dir)
    append_window(
        prior_stats,
        "bazaardb",
        WindowObservation(
            window_id="bazaardb:prior",
            observed_at="2026-04-29T12:00:00Z",
            items=[ItemWindowEvidence("Pufferfish", appearances=2, sample_count=5)],
        ),
    )
    save_stats(prior_stats, stats_dir)

    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch)

    pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=stats_dir,
        output_dir=tmp_path / "artifacts",
    )

    stats = load_stats("Karnok", stats_dir)
    history = stats.item_history("Pufferfish", "bazaardb")
    assert [row.window_id for row in history] == ["bazaardb:prior", "bazaardb:window"]


def test_live_empty_diff_short_circuits_pr(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "live_cron")
    tracker = make_tracker(tmp_path)
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(
        monkeypatch,
        {
            "schema_version": 1,
            "hero": "Karnok",
            "window_id": "w1",
            "source_window": {},
            "freeze_state": {},
            "source_health": [],
            "proposed_changes": {
                "archetype_updates": [],
                "archetype_additions": [],
                "archetype_removal_candidates": [],
                "item_removal_candidates": [],
                "archetype_reshuffles": [],
            },
            "weaker_signals": [],
            "noise": [],
        },
    )
    pr_calls = []
    comment_calls = []
    monkeypatch.setattr(pipeline, "_post_pr_comment", lambda *args: comment_calls.append(args))

    result = pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=tmp_path / "stats",
        output_dir=tmp_path / "artifacts",
        pr_action=lambda *args: pr_calls.append(args),
    )

    assert result.empty_diff is True
    assert pr_calls == []
    assert comment_calls == []


def test_live_cron_posts_supporting_evidence_comment(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "live_cron")
    tracker = make_tracker(tmp_path)
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch)
    git_calls = []

    def fake_git(repo, *args, check=True):
        git_calls.append(args)
        return subprocess_result(returncode=1 if args == ("diff", "--cached", "--quiet") else 0)

    def fake_run(args, **kwargs):
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess_result(returncode=0)
        return subprocess_result(returncode=0)

    comment_calls = []
    monkeypatch.setattr(pipeline, "_git", fake_git)
    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)
    monkeypatch.setattr(pipeline, "_post_pr_comment", lambda *args: comment_calls.append(args))

    result = pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=tmp_path / "stats",
        output_dir=tmp_path / "artifacts",
    )

    assert result.pr_invoked is True
    assert len(comment_calls) == 1
    hero, repo, diff_json, stats, branch = comment_calls[0]
    assert hero == "Karnok"
    assert repo == tracker
    assert diff_json["window_id"] == "w1"
    assert stats.hero == "Karnok"
    assert branch == "pipeline/Karnok"


def subprocess_result(*, returncode: int):
    return pipeline.subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr="")


def test_live_cron_dry_run_suppresses_pr(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "live_cron", dry_run=True)
    tracker = make_tracker(tmp_path)
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch)
    pr_calls = []

    result = pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=tmp_path / "stats",
        output_dir=tmp_path / "artifacts",
        pr_action=lambda *args: pr_calls.append(args),
    )

    assert result.pr_invoked is False
    assert pr_calls == []
    assert (tmp_path / "artifacts" / "Karnok_diff.json").exists()


def test_freeze_state_is_passed_to_evaluator(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "local_dry_run", freeze_removals_until="2099-01-01")
    tracker = make_tracker(tmp_path)
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch)

    pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=tmp_path / "stats",
        output_dir=tmp_path / "artifacts",
    )

    assert captured["state"].freeze_status("Karnok").active is True
