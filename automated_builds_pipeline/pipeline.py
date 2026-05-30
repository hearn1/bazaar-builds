"""Run the automated builds refresh pipeline for one hero."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from automated_builds_pipeline import applier, diff
from automated_builds_pipeline.deterministic_classifier import DeterministicClassifier
from automated_builds_pipeline.evaluator import load_catalog_items, evaluate_hero
from automated_builds_pipeline.hero_items import HeroItemOwnership, load_hero_item_ownership
from automated_builds_pipeline.pr_comment import render_pr_comment
from automated_builds_pipeline.proposal import render_proposal
from automated_builds_pipeline.sources import bazaar_builds_net, bazaardb, mobalytics
from automated_builds_pipeline.sources.base import HEALTHY, SKIPPED, FetchOptions, SourceFetchResult
from automated_builds_pipeline.readiness import REAL_CLASSIFIER_MODES
from automated_builds_pipeline.state import CuratorState, load_state
from automated_builds_pipeline.stats import (
    HeroStats,
    WindowObservation,
    _utc_now,
    append_window,
    load_stats,
    save_stats,
)

LOGGER = logging.getLogger(__name__)
SOURCE_BAZAARDB = "bazaardb"
SOURCE_MOBALYTICS_META = "mobalytics_meta_builds"
SOURCE_MOBALYTICS_ARTICLES = "mobalytics_build_articles"
SOURCE_BAZAAR_BUILDS_NET = "bazaar_builds_net"
LIVE_PHASE = "live_cron"
ClassifierMode = Literal["no_llm_shadow", "deterministic"]


@dataclass(frozen=True)
class PipelineResult:
    phase: str
    diff_path: Optional[Path] = None
    proposal_path: Optional[Path] = None
    pr_invoked: bool = False
    empty_diff: bool = False


PrAction = Callable[[str, Path, Path, dict[str, Any], Path, HeroStats], None]


def run(
    *,
    hero: str,
    state_file: Path,
    tracker_repo: Path,
    stats_dir: Path,
    output_dir: Path,
    bazaardb_retries: int = 2,
    no_bazaardb: bool = False,
    classifier_mode: ClassifierMode = "deterministic",
    pr_action: Optional[PrAction] = None,
) -> PipelineResult:
    state = load_state(state_file)
    if classifier_mode == "no_llm_shadow" and not _no_llm_shadow_allowed(state):
        raise ValueError("--classifier-mode no_llm_shadow is only allowed for dry-run or shadow_cron modes")
    if state.phase == "implementation":
        LOGGER.info("workflow not yet enabled")
        return PipelineResult(phase=state.phase)

    options = _fetch_options(state, tracker_repo)
    source_results = _fetch_sources(hero, tracker_repo, options, bazaardb_retries, no_bazaardb)
    ownership = load_hero_item_ownership(tracker_repo)
    source_results = _filter_source_results_for_hero_items(hero, source_results, ownership)
    current_run = _current_run(source_results, include_skipped={SOURCE_BAZAARDB} if no_bazaardb else set())

    stats = load_stats(hero, stats_dir)
    catalog_path = _catalog_path(hero, tracker_repo)
    catalog_items = load_catalog_items(catalog_path)
    evaluation = evaluate_hero(hero, catalog_items, stats, current_run, state)

    stats.last_classifier_mode = classifier_mode
    if classifier_mode in REAL_CLASSIFIER_MODES and stats.classifier_started_at is None:
        stats.classifier_started_at = _utc_now()

    if state.phase != "local_dry_run":
        for source, result in source_results.items():
            append_window(stats, source, result.observation)
        save_stats(stats, stats_dir)

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    classifier = None
    if classifier_mode == "deterministic":
        classifier = DeterministicClassifier(known_items_path=_names_file(tracker_repo))
        classifier.known_items.update(diff._all_catalog_names(catalog))
    diff_json = diff.generate_diff(hero, evaluation, catalog, classifier, classifier_mode=classifier_mode)

    output_dir.mkdir(parents=True, exist_ok=True)
    diff_path = output_dir / f"{hero}_diff.json"
    proposal_path = output_dir / f"{hero}_build_update_proposal.md"
    diff_path.write_text(json.dumps(diff_json, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    proposal_path.write_text(render_proposal(diff_json), encoding="utf-8")

    if state.phase == "local_dry_run":
        LOGGER.info("local_dry_run: artifacts written to %s", output_dir)
        return PipelineResult(state.phase, diff_path, proposal_path)
    if state.phase == "shadow_cron":
        LOGGER.info("shadow_cron: artifacts written for workflow upload")
        return PipelineResult(state.phase, diff_path, proposal_path)
    if state.dry_run:
        LOGGER.info("dry_run: artifacts written to %s", output_dir)
        return PipelineResult(state.phase, diff_path, proposal_path)

    if state.phase == LIVE_PHASE:
        if _empty_diff(diff_json):
            LOGGER.info("empty diff")
            return PipelineResult(state.phase, diff_path, proposal_path, empty_diff=True)
        action = pr_action or apply_catalog_pr
        action(hero, tracker_repo, proposal_path, diff_json, diff_path, stats)
        return PipelineResult(state.phase, diff_path, proposal_path, pr_invoked=True)

    raise ValueError(f"unsupported phase: {state.phase}")


def apply_catalog_pr(
    hero: str,
    tracker_repo: Path,
    proposal_path: Path,
    diff_json: dict[str, Any],
    diff_path: Path,
    stats: HeroStats,
) -> None:
    """Apply additive proposed_changes to the real <hero>_builds.json and open a
    PR whose only file diff is that single catalog file.

    Evidence rides as PR body (proposal markdown) + a supporting-evidence comment;
    no proposal/diff sidecar files are committed to the coach repo.
    """
    catalog_path = _catalog_path(hero, tracker_repo)
    schema = json.loads(_schema_path(tracker_repo).read_text(encoding="utf-8"))
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))

    # Fail closed: never touch a catalog we do not understand or that is already
    # invalid against the coach schema.
    applier.ensure_supported_schema_version(catalog)
    applier.validate_catalog(catalog, schema)

    result = applier.apply_proposed_changes(catalog, diff_json)

    # Fail closed: never write/commit/PR an invalid catalog. Aborts before any
    # git mutation.
    applier.validate_catalog(result.catalog, schema)

    serialized = applier.serialize_catalog(result.catalog)
    _atomic_write(catalog_path, serialized)

    branch = f"pipeline/{hero}"
    _git(tracker_repo, "checkout", "-B", branch)
    _git(tracker_repo, "add", str(catalog_path.relative_to(tracker_repo)))
    if _git(tracker_repo, "diff", "--cached", "--quiet", check=False).returncode == 0:
        LOGGER.info("catalog unchanged; no-op")
        return
    _git(tracker_repo, "commit", "-m", f"automated: {hero} catalog {_window_id(diff_json)}")
    _git(tracker_repo, "push", "--force", "origin", branch)

    title = f"[automated-builds] {hero} catalog"
    existing = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "number"],
        cwd=tracker_repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if existing.returncode == 0:
        subprocess.run(
            ["gh", "pr", "edit", branch, "--title", title, "--body-file", str(proposal_path)],
            cwd=tracker_repo,
            check=True,
        )
    else:
        subprocess.run(
            ["gh", "pr", "create", "--head", branch, "--title", title, "--body-file", str(proposal_path)],
            cwd=tracker_repo,
            check=True,
        )
    _post_pr_comment(hero, tracker_repo, diff_json, stats, branch)


def _post_pr_comment(
    hero: str,
    tracker_repo: Path,
    diff_json: dict[str, Any],
    stats: HeroStats,
    branch: str,
) -> None:
    body = render_pr_comment(diff_json, stats)
    with tempfile.TemporaryDirectory(prefix=f"{_hero_slug(hero)}-pr-comment-") as tmp_dir:
        body_file = Path(tmp_dir) / "supporting-evidence.md"
        body_file.write_text(body, encoding="utf-8")
        edited = subprocess.run(
            ["gh", "pr", "comment", branch, "--edit-last", "--body-file", str(body_file)],
            cwd=tracker_repo,
            text=True,
            capture_output=True,
            check=False,
        )
        if edited.returncode == 0:
            return
        subprocess.run(
            ["gh", "pr", "comment", branch, "--body-file", str(body_file)],
            cwd=tracker_repo,
            check=True,
        )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the automated builds refresh pipeline.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--hero", required=True)
    run_parser.add_argument("--state-file", type=Path, required=True)
    run_parser.add_argument("--tracker-repo", type=Path, required=True)
    run_parser.add_argument("--stats-dir", type=Path, required=True)
    run_parser.add_argument("--output-dir", type=Path, required=True)
    run_parser.add_argument("--bazaardb-retries", type=int, default=2)
    run_parser.add_argument("--no-bazaardb", action="store_true")
    run_parser.add_argument(
        "--classifier-mode",
        choices=("no_llm_shadow", "deterministic"),
        default="deterministic",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_arg_parser().parse_args(argv)
    if args.command == "run":
        run(
            hero=args.hero,
            state_file=args.state_file,
            tracker_repo=args.tracker_repo,
            stats_dir=args.stats_dir,
            output_dir=args.output_dir,
            bazaardb_retries=args.bazaardb_retries,
            no_bazaardb=args.no_bazaardb,
            classifier_mode=args.classifier_mode,
        )
        return 0
    return 2


def _fetch_sources(
    hero: str,
    tracker_repo: Path,
    options: FetchOptions,
    bazaardb_retries: int,
    no_bazaardb: bool,
) -> dict[str, SourceFetchResult]:
    results: dict[str, SourceFetchResult] = {}
    if no_bazaardb:
        results[SOURCE_BAZAARDB] = _skipped_result(SOURCE_BAZAARDB, options, ["operator skip"])
    else:
        results[SOURCE_BAZAARDB] = _fetch_bazaardb_with_retries(hero, options, bazaardb_retries)

    results[SOURCE_MOBALYTICS_META] = mobalytics.fetch_meta_builds(hero, options)
    results[SOURCE_MOBALYTICS_ARTICLES] = mobalytics.fetch_build_articles(options.article_slugs, options)
    results[SOURCE_BAZAAR_BUILDS_NET] = bazaar_builds_net.fetch_builds(hero, _bazaar_builds_category_url(hero), options)
    return results


def _fetch_bazaardb_with_retries(hero: str, options: FetchOptions, retries: int) -> SourceFetchResult:
    attempts = max(0, retries) + 1
    last_result = bazaardb.fetch_meta(hero, options)
    for _ in range(1, attempts):
        if last_result.status == HEALTHY or not _retryable_bazaardb(last_result):
            return last_result
        last_result = bazaardb.fetch_meta(hero, options)
    return last_result


def _current_run(
    source_results: dict[str, SourceFetchResult],
    *,
    include_skipped: set[str],
) -> list[SourceFetchResult]:
    return [
        result
        for source, result in source_results.items()
        if result.status != SKIPPED or source in include_skipped
    ]


def _filter_source_results_for_hero_items(
    hero: str,
    source_results: dict[str, SourceFetchResult],
    ownership: HeroItemOwnership,
) -> dict[str, SourceFetchResult]:
    return {
        source: _filter_source_result_for_hero_items(hero, result, ownership)
        for source, result in source_results.items()
    }


def _filter_source_result_for_hero_items(
    hero: str,
    result: SourceFetchResult,
    ownership: HeroItemOwnership,
) -> SourceFetchResult:
    kept_items = [item for item in result.observation.items if ownership.allows(hero, item.item)]
    filtered_count = len(result.observation.items) - len(kept_items)
    if filtered_count == 0:
        return result

    details = list(result.details)
    details.append(f"filtered {filtered_count} item(s) not owned by {hero}")
    observation = WindowObservation(
        window_id=result.observation.window_id,
        observed_at=result.observation.observed_at,
        items=kept_items,
        artifact_ref=result.observation.artifact_ref,
        health_status=result.status,
        details=details,
    )
    return SourceFetchResult(
        observation=observation,
        status=result.status,
        details=details,
        patch_label=result.patch_label,
    )


def _coach_builds_dir(tracker_repo: Path) -> Path:
    """Return the directory that contains builds_schema.json and *_builds.json catalogs.

    Checks repo root first (old layout), then builds/ (new layout after coach
    restructure). Falls through to root if neither has the schema anchor file so
    callers get a clean FileNotFoundError rather than a silent wrong path.
    """
    for candidate in [tracker_repo, tracker_repo / "builds"]:
        if (candidate / "builds_schema.json").exists():
            return candidate
    return tracker_repo


def _fetch_options(state: CuratorState, tracker_repo: Path) -> FetchOptions:
    observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return FetchOptions(
        observed_at=observed_at,
        expected_patch_label=state.expected_bazaardb_patch_label,
        catalog_dir=str(_coach_builds_dir(tracker_repo)),
        names_file=str(_names_file(tracker_repo)),
    )


def _catalog_path(hero: str, tracker_repo: Path) -> Path:
    path = _coach_builds_dir(tracker_repo) / f"{_hero_slug(hero)}_builds.json"
    if path.exists():
        return path
    raise FileNotFoundError(f"could not find catalog for {hero!r} in {tracker_repo}")


def _names_file(tracker_repo: Path) -> Path:
    return tracker_repo / "card_cache_names.txt"


def _schema_path(tracker_repo: Path) -> Path:
    return _coach_builds_dir(tracker_repo) / "builds_schema.json"


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def _bazaar_builds_category_url(hero: str) -> str:
    return f"https://bazaar-builds.net/category/builds/{_hero_slug(hero).replace('_', '-')}-builds/"


def _skipped_result(source: str, options: FetchOptions, details: list[str]) -> SourceFetchResult:
    observed_at = options.observed_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return SourceFetchResult(
        observation=WindowObservation(
            window_id=f"{source}:skipped",
            observed_at=observed_at,
            health_status=SKIPPED,
            details=details,
            items=[],
        ),
        status=SKIPPED,
        details=details,
    )


def _retryable_bazaardb(result: SourceFetchResult) -> bool:
    text = " ".join(result.details or result.observation.details).casefold()
    return any(token in text for token in ("cloudflare", "timeout", "transient", "browser", "playwright"))


def _empty_diff(diff_json: dict[str, Any]) -> bool:
    proposed = diff_json.get("proposed_changes", {})
    proposed_empty = all(not value for value in proposed.values()) if isinstance(proposed, dict) else True
    return proposed_empty and not diff_json.get("weaker_signals")


def _no_llm_shadow_allowed(state: CuratorState) -> bool:
    return state.phase == "shadow_cron" or state.dry_run


def _window_id(diff_json: dict[str, Any]) -> str:
    return str(diff_json.get("window_id") or "unknown")


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=check)


def _hero_slug(hero: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", hero.casefold()).strip("_")
    if not slug:
        raise ValueError("hero must produce a non-empty slug")
    return slug


if __name__ == "__main__":
    raise SystemExit(main())
