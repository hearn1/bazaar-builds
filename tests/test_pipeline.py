import json
from pathlib import Path

import pytest

from automated_builds_pipeline import pipeline
from automated_builds_pipeline.deterministic_classifier import DeterministicClassifier
from automated_builds_pipeline.evaluator import EvaluationResult
from automated_builds_pipeline.game_changes import GameChangeSignals
from automated_builds_pipeline.hero_items import HeroItemOwnership
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


BUILDS_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": [
        "schema_version",
        "hero",
        "season",
        "last_updated",
        "notes",
        "item_tier_list",
        "game_phases",
    ],
    "additionalProperties": True,
    "properties": {
        "schema_version": {"type": "integer", "minimum": 1},
        "hero": {"type": "string", "minLength": 1},
        "season": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
        "last_updated": {"type": "string"},
        "notes": {"type": "string"},
        "item_tier_list": {"type": "object", "additionalProperties": True},
        "game_phases": {
            "type": "object",
            "required": ["early", "early_mid", "late"],
            "additionalProperties": True,
            "properties": {
                "early": {"$ref": "#/definitions/early_phase"},
                "early_mid": {"$ref": "#/definitions/archetype_phase"},
                "late": {"$ref": "#/definitions/archetype_phase"},
            },
        },
    },
    "definitions": {
        "early_phase": {
            "type": "object",
            "required": ["day_range", "description", "universal_utility_items", "economy_items"],
            "additionalProperties": True,
            "properties": {
                "day_range": {"type": "string"},
                "description": {"type": "string"},
                "universal_utility_items": {"type": "array", "items": {"type": "string"}},
                "economy_items": {"type": "array", "items": {"type": "string"}},
            },
        },
        "archetype_phase": {
            "type": "object",
            "required": ["day_range", "description", "archetypes"],
            "additionalProperties": True,
            "properties": {
                "day_range": {"type": "string"},
                "description": {"type": "string"},
                "archetypes": {"type": "array", "items": {"$ref": "#/definitions/archetype"}},
            },
        },
        "archetype": {
            "type": "object",
            "required": ["name", "carry_items", "support_items"],
            "additionalProperties": True,
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "core_items": {"type": "array", "items": {"type": "string"}},
                "carry_items": {"type": "array", "items": {"type": "string"}},
                "support_items": {"type": "array", "items": {"type": "string"}},
                "timing_profile": {
                    "type": "string",
                    "enum": ["tempo", "scaling", "exodia", "neutral"],
                },
            },
        },
    },
}


def base_catalog() -> dict:
    return {
        "schema_version": 1,
        "hero": "Karnok",
        "season": 13,
        "last_updated": "2026-05-05",
        "notes": "test catalog",
        "item_tier_list": {"description": "test"},
        "game_phases": {
            "early": {
                "day_range": "Day 1-4",
                "description": "early",
                "universal_utility_items": [],
                "economy_items": [],
            },
            "early_mid": {
                "day_range": "Day 4-7",
                "description": "early_mid",
                "archetypes": [
                    {"name": "Axe", "carry_items": ["Battle Axe"], "support_items": []}
                ],
            },
            "late": {
                "day_range": "Day 7-13",
                "description": "late",
                "archetypes": [],
            },
        },
    }


def make_tracker(tmp_path: Path) -> Path:
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    (tracker / "card_cache_names.txt").write_text("Pufferfish\n", encoding="utf-8")
    (tracker / "builds_schema.json").write_text(json.dumps(BUILDS_SCHEMA), encoding="utf-8")
    (tracker / "karnok_builds.json").write_text(json.dumps(base_catalog()), encoding="utf-8")
    return tracker


def make_tracker_builds_subdir(tmp_path: Path) -> Path:
    """Tracker with catalogs and schema under builds/ (new coach layout)."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    builds = tracker / "builds"
    builds.mkdir()
    (tracker / "card_cache_names.txt").write_text("Pufferfish\n", encoding="utf-8")
    (builds / "builds_schema.json").write_text(json.dumps(BUILDS_SCHEMA), encoding="utf-8")
    (builds / "karnok_builds.json").write_text(json.dumps(base_catalog()), encoding="utf-8")
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
    def fake_evaluate(hero, catalog_items, stats, source_results, state, **kwargs):
        captured["source_results"] = list(source_results)
        captured["state"] = state
        captured["game_change_signals"] = kwargs.get("game_change_signals")
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


def minimal_diff_payload() -> dict:
    return {
        "schema_version": 1,
        "hero": "Karnok",
        "window_id": "w1",
        "source_window": {},
        "freeze_state": {"removals_frozen": False, "patch_label": None},
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
    }


def test_source_results_are_filtered_by_hero_item_ownership():
    result = SourceFetchResult(
        observation=WindowObservation(
            window_id="bazaardb:window",
            observed_at=OBSERVED_AT,
            items=[
                ItemWindowEvidence("Messenger Sparrow"),
                ItemWindowEvidence("Launch Tower"),
            ],
        ),
        status="healthy",
    )
    ownership = HeroItemOwnership(
        catalog_item_owners={"messenger sparrow": frozenset({"karnok"})}
    )

    filtered = pipeline._filter_source_results_for_hero_items(
        "Stelle",
        {"bazaardb": result},
        ownership,
    )

    items = [item.item for item in filtered["bazaardb"].observation.items]
    assert items == ["Launch Tower"]
    assert filtered["bazaardb"].details == ["filtered 1 item(s) not owned by Stelle"]


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


def test_local_dry_run_writes_artifacts_without_saving_stats(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "local_dry_run")
    tracker = make_tracker(tmp_path)
    stats_dir = tmp_path / "stats"
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch)
    monkeypatch.setattr(pipeline, "save_stats", lambda *args, **kwargs: pytest.fail("should not save stats"))

    result = pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=stats_dir,
        output_dir=tmp_path / "artifacts",
    )

    assert result.pr_invoked is False
    assert (tmp_path / "artifacts" / "Karnok_diff.json").exists()
    assert (tmp_path / "artifacts" / "Karnok_build_update_proposal.md").exists()
    assert not stats_path("Karnok", stats_dir).exists()


def test_shadow_cron_no_llm_shadow_bypasses_classifier_and_saves_stats(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "shadow_cron")
    tracker = make_tracker(tmp_path)
    catalog_before = (tracker / "karnok_builds.json").read_text(encoding="utf-8")
    stats_dir = tmp_path / "stats"
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    monkeypatch.setattr(pipeline, "DeterministicClassifier", lambda *args, **kwargs: pytest.fail("should not create classifier"))
    diff_calls = []

    def fake_generate_diff(hero, evaluation, catalog, classifier, *, classifier_mode):
        diff_calls.append({"classifier": classifier, "classifier_mode": classifier_mode})
        return minimal_diff_payload()

    monkeypatch.setattr(pipeline.diff, "generate_diff", fake_generate_diff)
    pr_calls = []

    pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=stats_dir,
        output_dir=tmp_path / "artifacts",
        classifier_mode="no_llm_shadow",
        pr_action=lambda *args: pr_calls.append(args),
    )

    assert diff_calls == [{"classifier": None, "classifier_mode": "no_llm_shadow"}]
    assert pr_calls == []
    assert (tracker / "karnok_builds.json").read_text(encoding="utf-8") == catalog_before
    assert stats_path("Karnok", stats_dir).exists()
    assert (tmp_path / "artifacts" / "Karnok_diff.json").exists()


def test_local_dry_run_no_llm_shadow_succeeds_without_classifier(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "local_dry_run")
    tracker = make_tracker(tmp_path)
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    monkeypatch.setattr(pipeline, "DeterministicClassifier", lambda *args, **kwargs: pytest.fail("should not create classifier"))
    diff_calls = []

    def fake_generate_diff(hero, evaluation, catalog, classifier, *, classifier_mode):
        diff_calls.append({"classifier": classifier, "classifier_mode": classifier_mode})
        return minimal_diff_payload()

    monkeypatch.setattr(pipeline.diff, "generate_diff", fake_generate_diff)

    result = pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=tmp_path / "stats",
        output_dir=tmp_path / "artifacts",
        classifier_mode="no_llm_shadow",
    )

    assert result.phase == "local_dry_run"
    assert diff_calls == [{"classifier": None, "classifier_mode": "no_llm_shadow"}]
    assert (tmp_path / "artifacts" / "Karnok_diff.json").exists()


def test_live_cron_without_dry_run_rejects_no_llm_shadow_before_work(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "live_cron", dry_run=False)
    tracker = make_tracker(tmp_path)
    monkeypatch.setattr(pipeline, "_fetch_sources", lambda *args, **kwargs: pytest.fail("should not fetch"))
    monkeypatch.setattr(pipeline.diff, "generate_diff", lambda *args, **kwargs: pytest.fail("should not diff"))

    with pytest.raises(ValueError, match="no_llm_shadow"):
        pipeline.run(
            hero="Karnok",
            state_file=state_file,
            tracker_repo=tracker,
            stats_dir=tmp_path / "stats",
            output_dir=tmp_path / "artifacts",
            classifier_mode="no_llm_shadow",
        )


def test_default_deterministic_mode_creates_classifier_and_uses_real_diff_mode(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "local_dry_run")
    tracker = make_tracker(tmp_path)
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    classifier_inits = []

    class FakeClassifier:
        def __init__(self, *, known_items_path, promote_cross_source=False):
            classifier_inits.append(known_items_path)
            self.known_items = set()

    diff_calls = []

    def fake_generate_diff(hero, evaluation, catalog, classifier, *, classifier_mode):
        diff_calls.append({"classifier": classifier, "classifier_mode": classifier_mode})
        return minimal_diff_payload()

    monkeypatch.setattr(pipeline, "DeterministicClassifier", FakeClassifier)
    monkeypatch.setattr(pipeline.diff, "generate_diff", fake_generate_diff)

    pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=tmp_path / "stats",
        output_dir=tmp_path / "artifacts",
    )

    assert classifier_inits == [tracker / "card_cache_names.txt"]
    assert diff_calls[0]["classifier_mode"] == "deterministic"
    assert isinstance(diff_calls[0]["classifier"], FakeClassifier)


def test_implementation_exits_before_fetch_save_or_artifact_work(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "implementation")
    tracker = make_tracker(tmp_path)
    monkeypatch.setattr(pipeline, "_fetch_sources", lambda *args, **kwargs: pytest.fail("should not fetch"))
    monkeypatch.setattr(pipeline, "evaluate_hero", lambda *args, **kwargs: pytest.fail("should not evaluate"))
    monkeypatch.setattr(pipeline.diff, "generate_diff", lambda *args, **kwargs: pytest.fail("should not diff"))
    monkeypatch.setattr(pipeline, "save_stats", lambda *args, **kwargs: pytest.fail("should not save stats"))

    result = pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=tmp_path / "stats",
        output_dir=tmp_path / "artifacts",
    )

    assert result.phase == "implementation"
    assert result.diff_path is None
    assert not (tmp_path / "artifacts").exists()
    assert not stats_path("Karnok", tmp_path / "stats").exists()


def test_workflow_persists_stats_only_for_shadow_and_live_phases():
    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "automated-builds-refresh.yml"
    text = workflow.read_text(encoding="utf-8")

    assert "steps.state.outputs.phase == 'shadow_cron' || steps.state.outputs.phase == 'live_cron'" in text
    assert "mock_llm:" not in text
    assert "--mock-llm" not in text
    assert "CLAUDE_API_KEY" not in text
    assert 'classifier_mode="deterministic"' in text
    assert '--classifier-mode "$classifier_mode"' in text


def test_run_merges_existing_stats_with_current_fetch_output(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "shadow_cron")
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
    patch_diff(monkeypatch, semantic_update_diff())
    git_calls = []

    def fake_git(repo, *args, check=True):
        git_calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return subprocess_result(returncode=0, stdout="coach-base\n")
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
    assert diff_json["diff_view"]["focus"] == "additions"
    assert stats.hero == "Karnok"
    assert branch == "pipeline/Karnok-additions"


def subprocess_result(*, returncode: int, stdout: str = ""):
    return pipeline.subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


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


def test_shadow_cron_deterministic_wires_classifier_and_records_started_once(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "shadow_cron")
    tracker = make_tracker(tmp_path)
    stats_dir = tmp_path / "stats"
    patch_fetchers(
        monkeypatch,
        bazaardb_results=[source_result("bazaardb"), source_result("bazaardb")],
    )
    captured = {}
    patch_evaluator(monkeypatch, captured)
    diff_calls = []

    def fake_generate_diff(hero, evaluation, catalog, classifier, *, classifier_mode):
        diff_calls.append({"classifier": classifier, "classifier_mode": classifier_mode})
        return minimal_diff_payload()

    monkeypatch.setattr(pipeline.diff, "generate_diff", fake_generate_diff)

    pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=stats_dir,
        output_dir=tmp_path / "artifacts",
        classifier_mode="deterministic",
        pr_action=lambda *args: None,
    )

    assert isinstance(diff_calls[0]["classifier"], DeterministicClassifier)
    assert diff_calls[0]["classifier_mode"] == "deterministic"
    stats_after_first = load_stats("Karnok", stats_dir)
    assert stats_after_first.last_classifier_mode == "deterministic"
    assert stats_after_first.classifier_started_at is not None
    first_started = stats_after_first.classifier_started_at

    # A second run must not overwrite classifier_started_at (set once).
    pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=stats_dir,
        output_dir=tmp_path / "artifacts",
        classifier_mode="deterministic",
        pr_action=lambda *args: None,
    )

    stats_after_second = load_stats("Karnok", stats_dir)
    assert stats_after_second.classifier_started_at == first_started
    assert stats_after_second.last_classifier_mode == "deterministic"


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


def test_default_game_change_signals_are_loaded_for_evaluator(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "local_dry_run")
    tracker = make_tracker(tmp_path)
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch)
    loaded = GameChangeSignals()
    monkeypatch.setattr(pipeline, "load_default_signals", lambda: loaded)

    pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=tmp_path / "stats",
        output_dir=tmp_path / "artifacts",
    )

    assert captured["game_change_signals"] is loaded


def test_explicit_game_change_signal_path_is_loaded_for_evaluator(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "local_dry_run")
    tracker = make_tracker(tmp_path)
    signal_path = tmp_path / "signals.json"
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch)
    loaded = GameChangeSignals()

    def fake_load_signals(path):
        assert path == signal_path
        return loaded

    monkeypatch.setattr(pipeline, "load_default_signals", lambda: pytest.fail("should load explicit path"))
    monkeypatch.setattr(pipeline, "load_signals", fake_load_signals)

    pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=tmp_path / "stats",
        output_dir=tmp_path / "artifacts",
        game_change_signal_path=signal_path,
    )

    assert captured["game_change_signals"] is loaded


def semantic_update_diff() -> dict:
    return {
        "schema_version": 1,
        "hero": "Karnok",
        "semantic_classification": True,
        "window_id": "w1",
        "source_window": {},
        "freeze_state": {"removals_frozen": False, "patch_label": None},
        "source_health": [],
        "proposed_changes": {
            "archetype_updates": [
                {
                    "phase": "early_mid",
                    "archetype": "Axe",
                    "missing_items": [
                        {"item": "Sawpike", "llm_classification": "carry"}
                    ],
                }
            ],
            "archetype_additions": [],
            "archetype_removal_candidates": [],
            "item_removal_candidates": [],
            "archetype_reshuffles": [],
        },
        "weaker_signals": [],
        "noise": [],
    }


def semantic_mixed_addition_retirement_diff() -> dict:
    payload = semantic_update_diff()
    payload["proposed_changes"]["item_removal_candidates"] = [
        {
            "phase": "early_mid",
            "archetype": "Axe",
            "item": "Bagpipes",
            "catalog_bucket": "support_items",
            "retirement_type": "support_item",
            "retirement_basis": "game_change_removed_card",
            "actionability": "item_removal_candidate",
            "removal_blocked_by": [],
            "freeze_blocked": False,
        }
    ]
    payload["proposed_changes"]["archetype_removal_candidates"] = [
        {
            "phase": "early_mid",
            "archetype": "Axe",
            "item": "Battle Axe",
            "catalog_bucket": "carry_items",
            "retirement_type": "whole_build_review",
            "retirement_basis": "bazaardb_absent_30_days",
            "actionability": "review_required",
            "affected_items": ["Battle Axe"],
        }
    ]
    return payload


def semantic_retirement_support_removal_diff() -> dict:
    payload = minimal_diff_payload()
    payload["semantic_classification"] = True
    payload["proposed_changes"]["item_removal_candidates"] = [
        {
            "phase": "early_mid",
            "archetype": "Axe",
            "item": "Bagpipes",
            "catalog_bucket": "support_items",
            "retirement_type": "support_item",
            "retirement_basis": "game_change_removed_card",
            "actionability": "item_removal_candidate",
            "removal_blocked_by": [],
            "freeze_blocked": False,
        }
    ]
    return payload


def semantic_review_only_retirement_diff() -> dict:
    payload = minimal_diff_payload()
    payload["semantic_classification"] = True
    payload["proposed_changes"]["archetype_removal_candidates"] = [
        {
            "phase": "early_mid",
            "archetype": "Axe",
            "item": "Battle Axe",
            "catalog_bucket": "carry_items",
            "retirement_type": "whole_build_review",
            "retirement_basis": "bazaardb_absent_30_days",
            "actionability": "review_required",
            "affected_items": ["Battle Axe"],
        }
    ]
    return payload


def test_partitioned_application_applies_additions_and_support_retirements():
    catalog = base_catalog()
    catalog["game_phases"]["early_mid"]["archetypes"][0]["support_items"] = ["Bagpipes"]

    result = pipeline._apply_partitioned_diff(catalog, semantic_mixed_addition_retirement_diff())
    axe = result.catalog["game_phases"]["early_mid"]["archetypes"][0]

    assert axe["carry_items"] == ["Battle Axe", "Sawpike"]
    assert axe["support_items"] == []
    assert any("added 'Sawpike'" in row for row in result.applied)
    assert any("removed 'Bagpipes'" in row for row in result.applied)
    assert any("archetype_removal_candidate 'Battle Axe'" in row for row in result.skipped)
    assert any("actionability='review_required'" in row for row in result.skipped)


def test_partitioned_application_review_only_retirement_surfaces_skip_reason():
    diff_json = minimal_diff_payload()
    diff_json["semantic_classification"] = True
    diff_json["proposed_changes"]["archetype_removal_candidates"] = [
        {
            "phase": "early_mid",
            "archetype": "Axe",
            "item": "Battle Axe",
            "catalog_bucket": "carry_items",
            "retirement_type": "whole_build_review",
            "actionability": "review_required",
        }
    ]

    result = pipeline._apply_partitioned_diff(base_catalog(), diff_json)

    assert result.changed is False
    assert result.applied == []
    assert any("archetype_removal_candidate 'Battle Axe'" in row for row in result.skipped)
    assert any("actionability='review_required'" in row for row in result.skipped)


def test_apply_catalog_pr_stages_only_catalog_and_uses_proposal_body(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "live_cron")
    tracker = make_tracker(tmp_path)
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch, semantic_update_diff())
    git_calls = []
    run_calls = []
    run_bodies = []

    def fake_git(repo, *args, check=True):
        git_calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return subprocess_result(returncode=0, stdout="coach-base\n")
        return subprocess_result(returncode=1 if args == ("diff", "--cached", "--quiet") else 0)

    def fake_run(args, **kwargs):
        run_calls.append(args)
        if "--body-file" in args:
            body_path = Path(args[args.index("--body-file") + 1])
            run_bodies.append(body_path.read_text(encoding="utf-8"))
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess_result(returncode=1)  # PR does not exist yet
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
    checkout_calls = [a for a in git_calls if a and a[0] == "checkout"]
    assert checkout_calls == [("checkout", "-B", "pipeline/Karnok-additions", "coach-base")]
    # Only the single catalog file is staged.
    add_calls = [a for a in git_calls if a and a[0] == "add"]
    assert add_calls == [("add", "karnok_builds.json")]
    # Catalog was actually mutated additively.
    catalog_after = json.loads((tracker / "karnok_builds.json").read_text(encoding="utf-8"))
    axe = catalog_after["game_phases"]["early_mid"]["archetypes"][0]
    assert axe["carry_items"] == ["Battle Axe", "Sawpike"]
    # No sidecar evidence files committed to the coach repo.
    assert not (tracker / "automated_builds_proposals").exists()
    # PR body is a focused proposal markdown temp file, not a committed sidecar.
    gh_create = next(a for a in run_calls if a[:3] == ["gh", "pr", "create"])
    assert gh_create[gh_create.index("--head") + 1] == "pipeline/Karnok-additions"
    assert gh_create[gh_create.index("--title") + 1] == "[automated-builds] Karnok catalog additions"
    assert "--body-file" in gh_create
    assert gh_create[gh_create.index("--body-file") + 1] != str(tmp_path / "artifacts" / "Karnok_build_update_proposal.md")
    assert "Sawpike" in run_bodies[0]
    assert "Bagpipes" not in run_bodies[0]
    assert all("automated_builds_proposals" not in str(part) for a in run_calls for part in a)
    assert len(comment_calls) == 1
    assert comment_calls[0][2]["diff_view"]["focus"] == "additions"
    assert comment_calls[0][4] == "pipeline/Karnok-additions"


def test_apply_catalog_pr_retirement_branch_when_support_removal_changes_bytes(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "live_cron")
    tracker = make_tracker(tmp_path)
    catalog = base_catalog()
    catalog["game_phases"]["early_mid"]["archetypes"][0]["support_items"] = ["Bagpipes"]
    (tracker / "karnok_builds.json").write_text(json.dumps(catalog), encoding="utf-8")
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch, semantic_retirement_support_removal_diff())
    git_calls = []
    run_calls = []
    run_bodies = []

    def fake_git(repo, *args, check=True):
        git_calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return subprocess_result(returncode=0, stdout="coach-base\n")
        return subprocess_result(returncode=1 if args == ("diff", "--cached", "--quiet") else 0)

    def fake_run(args, **kwargs):
        run_calls.append(args)
        if "--body-file" in args:
            body_path = Path(args[args.index("--body-file") + 1])
            run_bodies.append(body_path.read_text(encoding="utf-8"))
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess_result(returncode=1)
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
    checkout_calls = [a for a in git_calls if a and a[0] == "checkout"]
    assert checkout_calls == [("checkout", "-B", "pipeline/Karnok-retirements", "coach-base")]
    add_calls = [a for a in git_calls if a and a[0] == "add"]
    assert add_calls == [("add", "karnok_builds.json")]
    catalog_after = json.loads((tracker / "karnok_builds.json").read_text(encoding="utf-8"))
    axe = catalog_after["game_phases"]["early_mid"]["archetypes"][0]
    assert axe["support_items"] == []
    gh_create = next(a for a in run_calls if a[:3] == ["gh", "pr", "create"])
    assert gh_create[gh_create.index("--head") + 1] == "pipeline/Karnok-retirements"
    assert gh_create[gh_create.index("--title") + 1] == "[automated-builds] Karnok catalog retirements"
    assert "Bagpipes" in run_bodies[0]
    assert "Sawpike" not in run_bodies[0]
    assert len(comment_calls) == 1
    assert comment_calls[0][2]["diff_view"]["focus"] == "retirements"
    assert comment_calls[0][4] == "pipeline/Karnok-retirements"


def test_apply_catalog_pr_mixed_diff_opens_independent_addition_and_retirement_prs(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "live_cron")
    tracker = make_tracker(tmp_path)
    catalog = base_catalog()
    catalog["game_phases"]["early_mid"]["archetypes"][0]["support_items"] = ["Bagpipes"]
    (tracker / "karnok_builds.json").write_text(json.dumps(catalog), encoding="utf-8")
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch, semantic_mixed_addition_retirement_diff())
    git_calls = []
    run_calls = []
    run_bodies = []

    def fake_git(repo, *args, check=True):
        git_calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return subprocess_result(returncode=0, stdout="coach-base\n")
        return subprocess_result(returncode=1 if args == ("diff", "--cached", "--quiet") else 0)

    def fake_run(args, **kwargs):
        run_calls.append(args)
        if "--body-file" in args:
            body_path = Path(args[args.index("--body-file") + 1])
            branch = args[args.index("--head") + 1] if "--head" in args else args[3]
            run_bodies.append((branch, body_path.read_text(encoding="utf-8")))
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess_result(returncode=1)
        return subprocess_result(returncode=0)

    comment_calls = []
    monkeypatch.setattr(pipeline, "_git", fake_git)
    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)
    monkeypatch.setattr(pipeline, "_post_pr_comment", lambda *args: comment_calls.append(args))

    pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=tmp_path / "stats",
        output_dir=tmp_path / "artifacts",
    )

    checkout_calls = [a for a in git_calls if a and a[0] == "checkout"]
    assert checkout_calls == [
        ("checkout", "-B", "pipeline/Karnok-additions", "coach-base"),
        ("checkout", "-B", "pipeline/Karnok-retirements", "coach-base"),
    ]
    add_calls = [a for a in git_calls if a and a[0] == "add"]
    assert add_calls == [("add", "karnok_builds.json"), ("add", "karnok_builds.json")]
    created_heads = [a[a.index("--head") + 1] for a in run_calls if a[:3] == ["gh", "pr", "create"]]
    assert created_heads == ["pipeline/Karnok-additions", "pipeline/Karnok-retirements"]
    bodies = dict(run_bodies)
    assert "Sawpike" in bodies["pipeline/Karnok-additions"]
    assert "Bagpipes" not in bodies["pipeline/Karnok-additions"]
    assert "Bagpipes" in bodies["pipeline/Karnok-retirements"]
    assert "Sawpike" not in bodies["pipeline/Karnok-retirements"]
    assert [call[2]["diff_view"]["focus"] for call in comment_calls] == ["additions", "retirements"]
    assert [call[4] for call in comment_calls] == ["pipeline/Karnok-additions", "pipeline/Karnok-retirements"]


def test_apply_catalog_pr_review_only_retirement_skips_empty_pr(monkeypatch, tmp_path, caplog):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "live_cron")
    tracker = make_tracker(tmp_path)
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch, semantic_review_only_retirement_diff())
    git_calls = []
    run_calls = []

    def fake_git(repo, *args, check=True):
        git_calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return subprocess_result(returncode=0, stdout="coach-base\n")
        return subprocess_result(returncode=0)

    def fake_run(args, **kwargs):
        run_calls.append(args)
        return subprocess_result(returncode=0)

    comment_calls = []
    monkeypatch.setattr(pipeline, "_git", fake_git)
    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)
    monkeypatch.setattr(pipeline, "_post_pr_comment", lambda *args: comment_calls.append(args))

    with caplog.at_level("INFO", logger="automated_builds_pipeline.pipeline"):
        result = pipeline.run(
            hero="Karnok",
            state_file=state_file,
            tracker_repo=tracker,
            stats_dir=tmp_path / "stats",
            output_dir=tmp_path / "artifacts",
        )

    assert result.pr_invoked is True
    assert git_calls == [("rev-parse", "HEAD")]
    assert run_calls == []
    assert comment_calls == []
    assert "retirements catalog unchanged; no PR created: no catalog byte changes" in caplog.text
    assert "actionability='review_required'" in caplog.text


def test_local_dry_run_artifacts_remain_full_mixed_diff(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "local_dry_run")
    tracker = make_tracker(tmp_path)
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch, semantic_mixed_addition_retirement_diff())

    pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=tmp_path / "stats",
        output_dir=tmp_path / "artifacts",
    )

    artifact = json.loads((tmp_path / "artifacts" / "Karnok_diff.json").read_text(encoding="utf-8"))
    proposal = (tmp_path / "artifacts" / "Karnok_build_update_proposal.md").read_text(encoding="utf-8")

    assert "diff_view" not in artifact
    assert artifact["proposed_changes"]["archetype_updates"]
    assert artifact["proposed_changes"]["item_removal_candidates"]
    assert artifact["proposed_changes"]["archetype_removal_candidates"]
    assert "Sawpike" in proposal
    assert "Bagpipes" in proposal
    assert "Battle Axe" in proposal


def test_apply_catalog_pr_idempotent_noop_skips_commit_and_pr(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "live_cron")
    tracker = make_tracker(tmp_path)
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch, semantic_update_diff())
    git_calls = []
    run_calls = []

    def fake_git(repo, *args, check=True):
        git_calls.append(args)
        # Simulate "no staged changes" -> catalog already matches.
        return subprocess_result(returncode=0)

    def fake_run(args, **kwargs):
        run_calls.append(args)
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

    assert result.pr_invoked is True  # action was invoked
    assert not any(a and a[0] in {"commit", "push"} for a in git_calls)
    assert run_calls == []  # no gh pr view/create/edit
    assert comment_calls == []


def support_only_addition_diff() -> dict:
    """Non-empty diff whose only proposed change is a support-only archetype
    addition — the commitment guard rejects it, so the catalog is unchanged."""
    return {
        "schema_version": 1,
        "hero": "Karnok",
        "semantic_classification": True,
        "window_id": "w1",
        "source_window": {},
        "freeze_state": {"removals_frozen": False, "patch_label": None},
        "source_health": [],
        "proposed_changes": {
            "archetype_updates": [],
            "archetype_additions": [
                {
                    "tag": "Support Shape",
                    "candidate_phase": "late",
                    "candidate_core": [],
                    "candidate_support": [
                        {"item": "Pufferfish", "llm_classification": "support"}
                    ],
                }
            ],
            "archetype_removal_candidates": [],
            "item_removal_candidates": [],
            "archetype_reshuffles": [],
        },
        "weaker_signals": [],
        "noise": [],
    }


def test_apply_catalog_pr_support_only_addition_noop_logs_skip_reason(monkeypatch, tmp_path, caplog):
    # A non-empty diff that the applier fully rejects (support-only stub) must
    # not silently no-op: the run log has to surface why nothing was applied.
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "live_cron")
    tracker = make_tracker(tmp_path)
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch, support_only_addition_diff())
    run_calls = []

    def fake_git(repo, *args, check=True):
        # Rejected addition leaves the catalog byte-identical -> nothing staged.
        return subprocess_result(returncode=0)

    def fake_run(args, **kwargs):
        run_calls.append(args)
        return subprocess_result(returncode=0)

    monkeypatch.setattr(pipeline, "_git", fake_git)
    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)
    monkeypatch.setattr(pipeline, "_post_pr_comment", lambda *args: None)

    with caplog.at_level("INFO", logger="automated_builds_pipeline.pipeline"):
        result = pipeline.run(
            hero="Karnok",
            state_file=state_file,
            tracker_repo=tracker,
            stats_dir=tmp_path / "stats",
            output_dir=tmp_path / "artifacts",
        )

    assert result.pr_invoked is True  # action was invoked
    assert run_calls == []  # no gh pr create/edit/comment
    assert "no actionable changes" in caplog.text
    assert "support-only stub" in caplog.text


@pytest.mark.parametrize("phase,overrides", [
    ("local_dry_run", {}),
    ("shadow_cron", {}),
    ("live_cron", {"dry_run": True}),
])
def test_non_live_phases_do_not_mutate_catalog(monkeypatch, tmp_path, phase, overrides):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, phase, **overrides)
    tracker = make_tracker(tmp_path)
    before = (tracker / "karnok_builds.json").read_bytes()
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch, semantic_update_diff())

    pipeline.run(
        hero="Karnok",
        state_file=state_file,
        tracker_repo=tracker,
        stats_dir=tmp_path / "stats",
        output_dir=tmp_path / "artifacts",
        classifier_mode="deterministic",
        pr_action=lambda *args: None,
    )

    assert (tracker / "karnok_builds.json").read_bytes() == before


# --- coach builds/ layout resolver tests (Fixes #109) ---

def test_coach_builds_dir_returns_root_when_schema_at_root(tmp_path):
    tracker = make_tracker(tmp_path)
    assert pipeline._coach_builds_dir(tracker) == tracker


def test_coach_builds_dir_returns_builds_subdir_when_schema_there(tmp_path):
    tracker = make_tracker_builds_subdir(tmp_path)
    assert pipeline._coach_builds_dir(tracker) == tracker / "builds"


def test_schema_path_resolves_from_builds_subdir(tmp_path):
    tracker = make_tracker_builds_subdir(tmp_path)
    schema_path = pipeline._schema_path(tracker)
    assert schema_path == tracker / "builds" / "builds_schema.json"
    assert schema_path.exists()


def test_catalog_path_resolves_from_builds_subdir(tmp_path):
    tracker = make_tracker_builds_subdir(tmp_path)
    catalog_path = pipeline._catalog_path("Karnok", tracker)
    assert catalog_path == tracker / "builds" / "karnok_builds.json"
    assert catalog_path.exists()


def test_apply_catalog_pr_works_with_builds_subdir_layout(monkeypatch, tmp_path):
    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "live_cron")
    tracker = make_tracker_builds_subdir(tmp_path)
    patch_fetchers(monkeypatch)
    captured = {}
    patch_evaluator(monkeypatch, captured)
    patch_diff(monkeypatch, semantic_update_diff())
    git_calls = []
    run_calls = []

    def fake_git(repo, *args, check=True):
        git_calls.append(args)
        return subprocess_result(returncode=1 if args == ("diff", "--cached", "--quiet") else 0)

    def fake_run(args, **kwargs):
        run_calls.append(args)
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess_result(returncode=1)
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
    # Git add uses path relative to tracker root, so builds/karnok_builds.json.
    add_calls = [a for a in git_calls if a and a[0] == "add"]
    assert len(add_calls) == 1
    assert Path(add_calls[0][1]) == Path("builds/karnok_builds.json")
    catalog_after = json.loads((tracker / "builds" / "karnok_builds.json").read_text(encoding="utf-8"))
    axe = catalog_after["game_phases"]["early_mid"]["archetypes"][0]
    assert axe["carry_items"] == ["Battle Axe", "Sawpike"]


# --- #132: pipeline auto-discovers article slugs from meta-builds result ---


def test_fetch_sources_forwards_meta_state_slugs_to_build_articles(monkeypatch, tmp_path):
    """_fetch_sources extracts slugs from a healthy meta-builds result and passes
    them to fetch_build_articles; it does not call fetch_build_articles with an
    empty slug list when meta_state has discoverable slugs."""
    from automated_builds_pipeline.sources.base import FetchOptions
    from automated_builds_pipeline.sources.mobalytics import slugs_from_meta_builds_state

    # Build a meta-builds result that carries a meta_state with discoverable slugs.
    meta_state = {
        "theBazaarState": {
            "apollo": {
                "graphqlV2": {
                    "queries": [
                        {
                            "state": {
                                "data": [
                                    {
                                        "foo": [
                                            {"type": "link", "url": "/the-bazaar/builds/crunch-karnok-kripp"},
                                            {"type": "link", "url": "/the-bazaar/builds/blade-karnok-kripp"},
                                        ]
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        }
    }
    meta_result = source_result("mobalytics_meta_builds")
    meta_result.meta_state = meta_state

    slugs_seen = []

    def fake_fetch_build_articles(slugs, options):
        slugs_seen.extend(slugs)
        return source_result("mobalytics_build_articles")

    def fake_fetch_bazaardb(hero, options):
        return source_result("bazaardb")

    options = FetchOptions(observed_at=OBSERVED_AT)
    monkeypatch.setattr(pipeline.bazaardb, "fetch_meta", fake_fetch_bazaardb)
    monkeypatch.setattr(pipeline.mobalytics, "fetch_meta_builds", lambda hero, opts: meta_result)
    monkeypatch.setattr(pipeline.mobalytics, "fetch_build_articles", fake_fetch_build_articles)
    monkeypatch.setattr(
        pipeline.bazaar_builds_net,
        "fetch_builds",
        lambda hero, url, opts: source_result("bazaar_builds_net"),
    )

    state_file = tmp_path / "pipeline_state.json"
    write_state(state_file, "local_dry_run")
    tracker = make_tracker(tmp_path)
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

    assert "crunch-karnok-kripp" in slugs_seen
    assert "blade-karnok-kripp" in slugs_seen
