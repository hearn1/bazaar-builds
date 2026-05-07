import json

from automated_builds_pipeline.research import refresh_samples


def test_refresh_samples_selects_repeatable_sources_and_writes_under_source_dirs(tmp_path):
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    (tracker / "card_cache_names.txt").write_text("Pufferfish\n", encoding="utf-8")
    seen = []

    def refresher(source):
        def _refresh(output_dir, options, observed_at):
            seen.append((source, options.article_slugs))
            output_dir.mkdir(parents=True)
            path = output_dir / f"{source}.json"
            path.write_text(json.dumps({"source": source}), encoding="utf-8")
            return refresh_samples.RefreshResult(source, [path])

        return _refresh

    written = refresh_samples.refresh_samples(
        sources=["bazaardb", "bazaar_builds_net"],
        output_root=tmp_path / "samples",
        tracker_repo=tracker,
        article_slugs=["slug-one"],
        refreshers={
            "bazaardb": refresher("bazaardb"),
            "bazaar_builds_net": refresher("bazaar_builds_net"),
        },
    )

    assert [path.parent.name for path in written] == ["bazaardb", "bazaar_builds_net"]
    assert [entry[0] for entry in seen] == ["bazaardb", "bazaar_builds_net"]
    assert all(entry[1] == ["slug-one"] for entry in seen)


def test_refresh_samples_defaults_to_all_configured_live_sources(tmp_path):
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    (tracker / "card_cache_names.txt").write_text("Pufferfish\n", encoding="utf-8")
    seen = []

    def refresher(source):
        def _refresh(output_dir, options, observed_at):
            seen.append(source)
            output_dir.mkdir(parents=True)
            path = output_dir / f"{source}.json"
            path.write_text("{}", encoding="utf-8")
            return refresh_samples.RefreshResult(source, [path])

        return _refresh

    refresh_samples.refresh_samples(
        sources=None,
        output_root=tmp_path / "samples",
        tracker_repo=tracker,
        refreshers={source: refresher(source) for source in refresh_samples.ALL_SOURCES},
    )

    assert seen == list(refresh_samples.ALL_SOURCES)
