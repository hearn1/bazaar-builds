from __future__ import annotations

import json
from pathlib import Path

import pytest

from automated_builds_pipeline.game_change_maintenance import _format_markdown, main, run_maintenance_check
from automated_builds_pipeline.game_changes import parse_signals


def _make_signal(**overrides):
    base = {
        "id": "test-signal-2026-06-01",
        "type": "removed_card",
        "item": "Test Item",
        "effective_date": "2026-06-01",
        "source_url": "https://example.test/source",
        "note": "Test note.",
    }
    base.update(overrides)
    return base


def _make_doc(*signals):
    return {"schema_version": 1, "signals": list(signals)}


def _parse(*signals):
    return parse_signals(_make_doc(*signals))


# ---- clean file ----


def test_clean_active_only_file_exits_ok():
    sig = _make_signal(metadata={"status": "reviewed"})
    signals = _parse(sig)
    report = run_maintenance_check(signals)

    assert report["ok"] is True
    assert report["errors"] == []
    assert report["warnings"] == []
    assert report["totals"]["active"] == 1
    assert report["totals"]["deprecated"] == 0
    assert report["totals"]["superseded"] == 0


def test_clean_empty_file_exits_ok():
    signals = _parse()
    report = run_maintenance_check(signals)

    assert report["ok"] is True
    assert report["errors"] == []


def test_clean_deprecated_record_is_in_inactive_summary():
    dep = _make_signal(
        id="dep-1",
        metadata={"status": "deprecated", "deprecated_at": "2026-06-06", "status_note": "Corrected."},
    )
    signals = _parse(dep)
    report = run_maintenance_check(signals)

    assert report["ok"] is True
    assert report["totals"]["deprecated"] == 1
    assert "dep-1" in report["deprecated_ids"]
    assert report["totals"]["active"] == 0


def test_clean_superseded_record_exits_ok_when_superseded_by_exists():
    active = _make_signal(id="active-1")
    sup = _make_signal(
        id="sup-1",
        metadata={"status": "superseded", "superseded_at": "2026-06-06", "superseded_by": "active-1"},
    )
    signals = _parse(active, sup)
    report = run_maintenance_check(signals)

    assert report["ok"] is True
    assert report["totals"]["superseded"] == 1
    assert "sup-1" in report["superseded_ids"]


# ---- hard errors ----


def test_duplicate_ids_is_hard_error():
    sig1 = _make_signal(id="dup-id")
    sig2 = _make_signal(id="dup-id", item="Other Item")
    signals = _parse(sig1, sig2)
    report = run_maintenance_check(signals)

    assert report["ok"] is False
    assert any("duplicate id" in e and "dup-id" in e for e in report["errors"])


def test_dangling_superseded_by_is_hard_error():
    sup = _make_signal(
        id="sup-1",
        metadata={"status": "superseded", "superseded_by": "nonexistent-id"},
    )
    signals = _parse(sup)
    report = run_maintenance_check(signals)

    assert report["ok"] is False
    assert any("nonexistent-id" in e for e in report["errors"])


def test_self_supersession_is_hard_error():
    sup = _make_signal(
        id="self-sup",
        metadata={"status": "superseded", "superseded_by": "self-sup"},
    )
    signals = _parse(sup)
    report = run_maintenance_check(signals)

    assert report["ok"] is False
    assert any("itself" in e for e in report["errors"])


def test_supersession_cycle_is_hard_error():
    a = _make_signal(id="a", metadata={"status": "superseded", "superseded_by": "b"})
    b = _make_signal(id="b", item="Other", metadata={"status": "superseded", "superseded_by": "a"})
    signals = _parse(a, b)
    report = run_maintenance_check(signals)

    assert report["ok"] is False
    assert any("cycle" in e for e in report["errors"])


def test_superseded_without_superseded_by_is_hard_error():
    sup = _make_signal(id="sup-1", metadata={"status": "superseded"})
    signals = _parse(sup)
    report = run_maintenance_check(signals)

    assert report["ok"] is False
    assert any("missing superseded_by" in e for e in report["errors"])


# ---- warnings ----


def test_deprecated_without_status_note_emits_warning():
    dep = _make_signal(id="dep-1", metadata={"status": "deprecated", "deprecated_at": "2026-06-06"})
    signals = _parse(dep)
    report = run_maintenance_check(signals)

    assert report["ok"] is True
    assert any("status_note" in w for w in report["warnings"])


def test_deprecated_without_deprecated_at_emits_warning():
    dep = _make_signal(id="dep-1", metadata={"status": "deprecated", "status_note": "Reason."})
    signals = _parse(dep)
    report = run_maintenance_check(signals)

    assert report["ok"] is True
    assert any("deprecated_at" in w for w in report["warnings"])


def test_superseded_without_superseded_at_emits_warning():
    active = _make_signal(id="active-1")
    sup = _make_signal(id="sup-1", metadata={"status": "superseded", "superseded_by": "active-1"})
    signals = _parse(active, sup)
    report = run_maintenance_check(signals)

    assert report["ok"] is True
    assert any("superseded_at" in w for w in report["warnings"])


def test_proposed_record_emits_warning():
    sig = _make_signal(id="prop-1", metadata={"status": "proposed"})
    signals = _parse(sig)
    report = run_maintenance_check(signals)

    assert report["ok"] is True
    assert any("proposed" in w for w in report["warnings"])
    assert "prop-1" in report["proposed_ids"]


def test_unknown_metadata_status_emits_warning():
    sig = _make_signal(id="sig-1", metadata={"status": "mystery_value"})
    signals = _parse(sig)
    report = run_maintenance_check(signals)

    assert report["ok"] is True
    assert any("mystery_value" in w for w in report["warnings"])
    assert "sig-1" in report["unknown_status_ids"]


def test_superseding_record_itself_inactive_emits_warning():
    sup_a = _make_signal(id="sup-a", metadata={"status": "superseded", "superseded_by": "sup-b"})
    sup_b = _make_signal(id="sup-b", item="Other Item", metadata={"status": "deprecated", "deprecated_at": "2026-06-06", "status_note": "x"})
    signals = _parse(sup_a, sup_b)
    report = run_maintenance_check(signals)

    assert any("also inactive" in w for w in report["warnings"])


# ---- output formats ----


def test_markdown_output_contains_summary_table():
    sig = _make_signal(metadata={"status": "reviewed"})
    signals = _parse(sig)
    report = run_maintenance_check(signals)
    md = _format_markdown(report)

    assert "## Summary" in md
    assert "| Total |" in md
    assert "| Active |" in md
    assert "| Deprecated |" in md
    assert "| Superseded |" in md


def test_markdown_output_lists_errors():
    sup = _make_signal(id="sup-1", metadata={"status": "superseded", "superseded_by": "ghost"})
    signals = _parse(sup)
    report = run_maintenance_check(signals)
    md = _format_markdown(report)

    assert "## Errors" in md
    assert "ERROR" in md


def test_json_output_is_stable(tmp_path):
    sig = _make_signal(metadata={"status": "reviewed"})
    signals = _parse(sig)
    report = run_maintenance_check(signals)
    json_str = json.dumps(report, indent=2)
    parsed = json.loads(json_str)

    assert parsed["ok"] is True
    assert "totals" in parsed
    assert "errors" in parsed
    assert "warnings" in parsed


# ---- main() CLI entrypoint ----


def test_main_exits_zero_for_clean_file(tmp_path):
    sig = _make_signal(metadata={"status": "reviewed"})
    path = tmp_path / "signals.json"
    path.write_text(json.dumps({"schema_version": 1, "signals": [sig]}), encoding="utf-8")

    rc = main(["--signals", str(path), "--format", "markdown"])

    assert rc == 0


def test_main_exits_nonzero_for_hard_error(tmp_path):
    dup_a = _make_signal(id="dup")
    dup_b = _make_signal(id="dup", item="Other")
    path = tmp_path / "signals.json"
    path.write_text(json.dumps({"schema_version": 1, "signals": [dup_a, dup_b]}), encoding="utf-8")

    rc = main(["--signals", str(path)])

    assert rc != 0


def test_main_json_output_written_to_file(tmp_path):
    sig = _make_signal(metadata={"status": "reviewed"})
    signals_path = tmp_path / "signals.json"
    signals_path.write_text(json.dumps({"schema_version": 1, "signals": [sig]}), encoding="utf-8")
    out_path = tmp_path / "report.json"

    rc = main(["--signals", str(signals_path), "--format", "json", "--output", str(out_path)])

    assert rc == 0
    assert out_path.is_file()
    parsed = json.loads(out_path.read_text(encoding="utf-8"))
    assert parsed["ok"] is True
