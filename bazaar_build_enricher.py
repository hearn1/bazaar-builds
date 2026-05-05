"""Collect Bazaar Builds evidence for human-reviewed build catalog updates.

This tool is intentionally read-only with respect to ``*_builds.json``. It
fetches category/index pages, optionally tries individual post pages, accepts
manual fallback records, and emits a normalized JSON artifact for review.

Example:
    python bazaar_build_enricher.py https://bazaar-builds.net/category/builds/dooley-builds/ --hero Dooley --days 30 --output artifacts/dooley_bazaar_builds_summary.json

    # Explicit paths (required when not running from a sibling bazaar_tracker checkout):
    python bazaar_build_enricher.py https://bazaar-builds.net/category/builds/karnok-builds/ --hero Karnok \\
        --catalog-dir /path/to/bazaar_tracker --names-file /path/to/bazaar_tracker/card_cache_names.txt
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

DEFAULT_TIMEOUT = 15
USER_AGENT = "BazaarTracker/BuildEnricher (+human-reviewed catalog helper)"

# Default to a sibling bazaar_tracker checkout for ergonomic curator use.
_DEFAULT_TRACKER_ROOT = Path(__file__).parent.parent / "bazaar_tracker"

ITEM_ALIASES = {
    "YLW-MANTIS": "YLW-M4NT1S",
    "YELLOW MANTIS": "YLW-M4NT1S",
    "YLW MANTIS": "YLW-M4NT1S",
    "NANOBOTS": "Nanobot",
}

KNOWN_HEROES = frozenset(
    h.casefold() for h in ("Dooley", "Vanessa", "Pygmalien", "Mak", "Karnok", "Jules", "Stelle")
)
WIN_RECORD_RE = re.compile(r"\b\d+[-–]\d+\b|\b\d+\s*[Ww]in\b", re.IGNORECASE)
GENERIC_TAG_PHRASES = frozenset(["submit your", "build finder", "category"])


@dataclass
class BuildRecord:
    url: Optional[str] = None
    title: Optional[str] = None
    date: Optional[str] = None
    hero: Optional[str] = None
    tag: Optional[str] = None
    items: list[str] = field(default_factory=list)
    source: str = "unknown"
    snippet: Optional[str] = None
    fetch_status: str = "not_attempted"
    fetch_error: Optional[str] = None

    def key(self) -> str:
        return self.url or f"{self.title}|{self.date}|{self.source}"


@dataclass
class ManualLoadResult:
    records: list[BuildRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class SimpleHTMLTextParser(HTMLParser):
    """Small structured HTML extractor for anchors, dates, JSON-LD, and text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict] = []
        self.dates: list[str] = []
        self.scripts: list[str] = []
        self.text_chunks: list[str] = []
        self._tag_stack: list[str] = []
        self._current_link: Optional[dict] = None
        self._current_script: Optional[list[str]] = None
        self._script_type: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr = dict(attrs)
        self._tag_stack.append(tag)
        if tag == "a" and attr.get("href"):
            self._current_link = {"href": attr["href"], "text": ""}
        elif tag == "time":
            value = attr.get("datetime")
            if value:
                self.dates.append(value)
        elif tag == "script":
            self._script_type = attr.get("type")
            self._current_script = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_link is not None:
            text = clean_space(self._current_link.get("text"))
            if text:
                self._current_link["text"] = text
                self.links.append(self._current_link)
            self._current_link = None
        elif tag == "script" and self._current_script is not None:
            if self._script_type == "application/ld+json":
                self.scripts.append("".join(self._current_script))
            self._current_script = None
            self._script_type = None
        if self._tag_stack:
            self._tag_stack.pop()

    def handle_data(self, data: str) -> None:
        text = clean_space(data)
        if self._current_script is not None:
            self._current_script.append(data)
            return
        if not text:
            return
        if self._current_link is not None:
            self._current_link["text"] += f" {text}"
        self.text_chunks.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.text_chunks)


def clean_space(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    raw = clean_space(value)
    candidates = [
        raw,
        raw[:10],
        raw.replace("Published on ", ""),
        raw.replace("Updated on ", ""),
    ]
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            return datetime.fromisoformat(candidate.replace("Z", "+00:00")).date()
        except ValueError:
            pass
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def iso_date(value: Optional[str]) -> Optional[str]:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else None


def fetch_url(url: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[Optional[str], str, Optional[str]]:
    try:
        import requests
    except ImportError as exc:
        return None, "requests_unavailable", str(exc)

    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
        if response.status_code >= 400:
            return None, f"http_{response.status_code}", f"HTTP {response.status_code}"
        return response.text, f"http_{response.status_code}", None
    except requests.RequestException as exc:
        return None, "fetch_failed", str(exc)


def parse_html(html: str) -> SimpleHTMLTextParser:
    parser = SimpleHTMLTextParser()
    parser.feed(html or "")
    return parser


def load_known_items(catalog_dir: Path, names_file: Path) -> set[str]:
    items: set[str] = set()
    for path in catalog_dir.glob("*_builds.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        collect_items_from_build_catalog(data, items)

    if names_file.is_file():
        raw = names_file.read_text(encoding="utf-8", errors="ignore")
        start = raw.find("[")
        if start >= 0:
            try:
                names = json.loads(raw[start:])
                items.update(name.strip() for name in names if isinstance(name, str) and name.strip())
            except json.JSONDecodeError:
                pass

    return {normalize_item_name(item, items) for item in items if not item.startswith("[")}


def collect_items_from_build_catalog(data: dict, items: set[str]) -> None:
    tier_list = data.get("item_tier_list", {})
    if isinstance(tier_list, dict):
        for value in tier_list.values():
            if isinstance(value, list):
                items.update(str(item) for item in value if item)
    phases = data.get("game_phases", {})
    if isinstance(phases, dict):
        for phase in phases.values():
            if not isinstance(phase, dict):
                continue
            for key in ("universal_utility_items", "economy_items"):
                value = phase.get(key, [])
                if isinstance(value, list):
                    items.update(str(item) for item in value if item)
            for arch in phase.get("archetypes", []):
                if not isinstance(arch, dict):
                    continue
                for key in ("condition_items", "core_items", "carry_items", "support_items"):
                    value = arch.get(key, [])
                    if isinstance(value, list):
                        items.update(str(item) for item in value if item and not str(item).startswith("TODO"))


def item_lookup(known_items: Iterable[str]) -> dict[str, str]:
    lookup = {}
    for item in known_items:
        lookup[canonical_key(item)] = item
    for alias, target in ITEM_ALIASES.items():
        lookup[canonical_key(alias)] = target
    return lookup


def canonical_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def normalize_item_name(value: str, known_items: Optional[set[str]] = None) -> str:
    cleaned = clean_space(value).strip(" -*:;,.")
    if not cleaned:
        return ""
    alias = ITEM_ALIASES.get(cleaned.upper())
    if alias:
        return alias
    if known_items:
        lookup = item_lookup(known_items)
        return lookup.get(canonical_key(cleaned), cleaned)
    return cleaned


def infer_hero_from_url_or_title(url: Optional[str], title: Optional[str]) -> Optional[str]:
    haystack = f"{url or ''} {title or ''}".casefold()
    for hero in ("Dooley", "Vanessa", "Pygmalien", "Mak", "Karnok"):
        if hero.casefold() in haystack:
            return hero
    return None


def infer_tag(title: Optional[str], url: Optional[str] = None, hero: Optional[str] = None) -> str:
    title_text = clean_space(title)
    patterns = [
        r"(?P<tag>[\w.&' -]+?\s+Build)\b",
        r"Best\s+(?P<tag>[\w.&' -]+?)\s+Build",
        r"(?P<tag>[\w.&' -]+?)\s+(?:Guide|Deck)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, title_text, flags=re.IGNORECASE)
        if match:
            return tidy_tag(match.group("tag"), hero)

    path = urlparse(url or "").path.replace("-", " ")
    match = re.search(r"([a-z0-9.&' ]+?) build", path, flags=re.IGNORECASE)
    if match:
        return tidy_tag(match.group(1) + " Build", hero)
    return "Uncategorized"


def tidy_tag(value: str, hero: Optional[str] = None) -> str:
    tag = clean_space(value).strip(" -|")
    if hero:
        tag = re.sub(rf"\b{re.escape(hero)}\b", "", tag, flags=re.IGNORECASE)
        tag = clean_space(tag)
    tag = WIN_RECORD_RE.sub("", tag)
    tag = clean_space(tag).strip(" -|")
    if not tag:
        return "Uncategorized"
    if not tag.casefold().endswith("build"):
        tag = f"{tag} Build"
    return tag


def extract_category_records(category_url: str, html: str, hero: Optional[str], limit: int) -> list[BuildRecord]:
    parser = parse_html(html)
    records: list[BuildRecord] = []
    seen = set()
    for link in parser.links:
        href = str(link.get("href") or "")
        text = clean_space(link.get("text"))
        if not href or not text:
            continue
        absolute = urljoin(category_url, href)
        path = urlparse(absolute).path
        if "/build" not in path.casefold() and "build" not in text.casefold():
            continue
        if absolute.rstrip("/") == category_url.rstrip("/"):
            continue
        key = absolute.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        record_hero = hero or infer_hero_from_url_or_title(absolute, text)
        records.append(BuildRecord(
            url=absolute,
            title=text,
            hero=record_hero,
            tag=infer_tag(text, absolute, record_hero),
            source="category",
            snippet=text,
        ))
        if len(records) >= limit:
            break

    text_dates = re.findall(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}\b", parser.text)
    dates = [iso_date(value) for value in parser.dates + text_dates]
    dates = [value for value in dates if value]
    for index, record in enumerate(records):
        if index < len(dates):
            record.date = dates[index]
    return records


def extract_json_ld_records(html: str, base_url: str, hero: Optional[str], known_items: set[str]) -> list[BuildRecord]:
    parser = parse_html(html)
    records: list[BuildRecord] = []
    for script in parser.scripts:
        try:
            data = json.loads(script)
        except json.JSONDecodeError:
            continue
        for node in flatten_json_ld(data):
            if not isinstance(node, dict):
                continue
            title = node.get("headline") or node.get("name")
            url = node.get("url") or base_url
            text = " ".join(str(node.get(key) or "") for key in ("articleBody", "description", "text"))
            items = extract_items_from_text(text, known_items)
            if title or items:
                record_hero = hero or infer_hero_from_url_or_title(url, title)
                records.append(BuildRecord(
                    url=urljoin(base_url, str(url)),
                    title=clean_space(title),
                    date=iso_date(str(node.get("datePublished") or node.get("dateModified") or "")),
                    hero=record_hero,
                    tag=infer_tag(str(title or ""), str(url), record_hero),
                    items=items,
                    source="json_ld",
                    snippet=clean_space(node.get("description")),
                ))
    return records


def flatten_json_ld(data):
    if isinstance(data, list):
        for item in data:
            yield from flatten_json_ld(item)
    elif isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            yield from flatten_json_ld(graph)
        yield data


def enrich_post(record: BuildRecord, known_items: set[str], timeout: int) -> BuildRecord:
    if not record.url:
        return record
    html, status, error = fetch_url(record.url, timeout)
    record.fetch_status = status
    record.fetch_error = error
    if not html:
        return record

    parser = parse_html(html)
    json_ld_records = extract_json_ld_records(html, record.url, record.hero, known_items)
    record.date = record.date or next((row.date for row in json_ld_records if row.date), None)
    rich = next((row for row in json_ld_records if row.items), None)
    if rich:
        record.items = merge_unique(record.items, rich.items)
        record.date = record.date or rich.date
        record.title = record.title or rich.title
        record.tag = record.tag or rich.tag
        record.snippet = record.snippet or rich.snippet

    text_items = extract_items_from_text(parser.text, known_items)
    record.items = merge_unique(record.items, text_items)
    if not record.date:
        dates = [iso_date(value) for value in parser.dates]
        record.date = next((value for value in dates if value), None)
    return record


def extract_items_from_text(text: str, known_items: set[str]) -> list[str]:
    found = []
    lookup = item_lookup(known_items)
    lines = re.split(r"[\n\r]+|(?:\s{2,})", text or "")
    for line in lines:
        cleaned = clean_space(re.sub(r"^[\-*•\d.)\s]+", "", line))
        if not cleaned:
            continue
        exact = lookup.get(canonical_key(cleaned))
        if exact:
            found.append(exact)
            continue
        for part in re.split(r"[,;/|+]", cleaned):
            normalized = normalize_item_name(part, known_items)
            if normalized and canonical_key(normalized) in lookup:
                found.append(lookup[canonical_key(normalized)])

    lowered = f" {text.casefold()} "
    for key, item in lookup.items():
        if len(key) < 5:
            continue
        escaped = re.escape(item.casefold())
        if re.search(rf"(?<![\w-]){escaped}(?![\w-])", lowered):
            found.append(item)
    return merge_unique([], found)


def merge_unique(left: list[str], right: list[str]) -> list[str]:
    seen = set()
    merged = []
    for item in left + right:
        if item and item not in seen:
            seen.add(item)
            merged.append(item)
    return merged


def load_manual_records(paths: list[str], hero: Optional[str], known_items: set[str]) -> ManualLoadResult:
    result = ManualLoadResult()
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            result.warnings.append(f"Manual fallback file not found: {path}")
            continue
        text = path.read_text(encoding="utf-8")
        try:
            if path.suffix.casefold() == ".json":
                result.records.extend(records_from_json(text, hero, known_items, source=f"manual:{path.name}"))
            else:
                result.records.extend(records_from_text(text, hero, known_items, source=f"manual:{path.name}"))
        except (OSError, json.JSONDecodeError) as exc:
            result.warnings.append(f"Manual fallback file could not be parsed: {path} ({exc})")
    return result


def records_from_json(text: str, hero: Optional[str], known_items: set[str], source: str = "manual") -> list[BuildRecord]:
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("records") or data.get("builds") or [data]
    records = []
    for row in data:
        if not isinstance(row, dict):
            continue
        title = clean_space(row.get("title"))
        record_hero = clean_space(row.get("hero")) or hero or infer_hero_from_url_or_title(row.get("url"), title)
        raw_items = row.get("items") or []
        if isinstance(raw_items, str):
            items = extract_items_from_text(raw_items, known_items)
        else:
            items = [normalize_item_name(str(item), known_items) for item in raw_items]
        snippet = clean_space(row.get("snippet") or row.get("text"))
        items = merge_unique(items, extract_items_from_text(snippet, known_items))
        records.append(BuildRecord(
            url=clean_space(row.get("url")) or None,
            title=title or None,
            date=iso_date(str(row.get("date") or "")),
            hero=record_hero or None,
            tag=clean_space(row.get("tag") or row.get("category")) or infer_tag(title, row.get("url"), record_hero),
            items=items,
            source=source,
            snippet=snippet or None,
            fetch_status="manual",
        ))
    return records


def records_from_text(text: str, hero: Optional[str], known_items: set[str], source: str = "manual") -> list[BuildRecord]:
    records = []
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    for block in blocks:
        meta = {}
        item_lines = []
        for line in block.splitlines():
            match = re.match(r"^(url|title|date|hero|tag|category)\s*:\s*(.+)$", line.strip(), flags=re.IGNORECASE)
            if match:
                meta[match.group(1).casefold()] = match.group(2).strip()
            else:
                item_lines.append(line)
        title = clean_space(meta.get("title"))
        record_hero = clean_space(meta.get("hero")) or hero or infer_hero_from_url_or_title(meta.get("url"), title)
        item_text = "\n".join(item_lines)
        records.append(BuildRecord(
            url=clean_space(meta.get("url")) or None,
            title=title or None,
            date=iso_date(meta.get("date")),
            hero=record_hero or None,
            tag=clean_space(meta.get("tag") or meta.get("category")) or infer_tag(title, meta.get("url"), record_hero),
            items=extract_items_from_text(item_text, known_items),
            source=source,
            snippet=clean_space(item_text)[:300] or None,
            fetch_status="manual",
        ))
    return records


def filter_records(
    records: list[BuildRecord],
    since: Optional[date],
    hero: Optional[str] = None,
) -> list[BuildRecord]:
    hero_lower = (hero or "").casefold()
    kept = []
    for record in records:
        if since:
            record_date = parse_date(record.date)
            if record_date is not None and record_date < since:
                continue
        if hero_lower:
            inferred = infer_hero_from_url_or_title(record.url, record.title)
            if inferred and inferred.casefold() != hero_lower:
                continue
        tag_lower = (record.tag or "").casefold()
        if any(phrase in tag_lower for phrase in GENERIC_TAG_PHRASES):
            continue
        kept.append(record)
    return kept


def dedupe_records(records: list[BuildRecord]) -> list[BuildRecord]:
    by_key: dict[str, BuildRecord] = {}
    for record in records:
        key = record.key()
        existing = by_key.get(key)
        if not existing:
            by_key[key] = record
            continue
        existing.items = merge_unique(existing.items, record.items)
        existing.date = existing.date or record.date
        existing.title = existing.title or record.title
        existing.hero = existing.hero or record.hero
        existing.tag = existing.tag if existing.tag != "Uncategorized" else record.tag
        existing.snippet = existing.snippet or record.snippet
        if existing.fetch_status == "not_attempted":
            existing.fetch_status = record.fetch_status
        existing.fetch_error = existing.fetch_error or record.fetch_error
    return list(by_key.values())


def build_summary(
    records: list[BuildRecord],
    *,
    category_url: str,
    hero: Optional[str],
    since: Optional[date],
    days: Optional[int],
    known_items: set[str],
    warnings: Optional[list[str]] = None,
) -> dict:
    groups = defaultdict(list)
    for record in records:
        record.tag = record.tag or infer_tag(record.title, record.url, record.hero or hero)
        groups[record.tag].append(record)

    group_summaries = []
    for tag, rows in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
        item_counts = Counter()
        for row in rows:
            item_counts.update(row.items)
        sample_count = len(rows)
        core_threshold = max(2, int(sample_count * 0.6 + 0.999)) if sample_count >= 3 else sample_count
        support_threshold = max(2, int(sample_count * 0.3 + 0.999)) if sample_count >= 4 else 2
        candidate_core = [item for item, count in item_counts.most_common() if count >= core_threshold]
        candidate_support = [
            item for item, count in item_counts.most_common()
            if item not in candidate_core and count >= support_threshold
        ]
        dated = [parse_date(row.date) for row in rows if parse_date(row.date)]
        date_range = {
            "start": min(dated).isoformat() if dated else None,
            "end": max(dated).isoformat() if dated else None,
        }
        group_summaries.append({
            "tag": tag,
            "sample_count": sample_count,
            "date_range": date_range,
            "item_frequencies": [
                {"item": item, "count": count, "frequency": round(count / sample_count, 3)}
                for item, count in item_counts.most_common()
            ],
            "candidate_core_items": candidate_core,
            "candidate_support_items": candidate_support,
            "likely_archetype_notes": infer_notes(tag, sample_count, candidate_core, candidate_support),
            "builds": [record_to_dict(row) for row in rows],
        })

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "category_url": category_url,
            "site": "bazaar-builds.net",
        },
        "filters": {
            "hero": hero,
            "days": days,
            "since": since.isoformat() if since else None,
        },
        "known_item_count": len(known_items),
        "record_count": len(records),
        "warnings": warnings or [],
        "groups": group_summaries,
        "records": [record_to_dict(row) for row in records],
        "review_guidance": [
            "This artifact is evidence for a human review pass; it does not update any *_builds.json file.",
            "Compare candidate_core_items and candidate_support_items against the matching hero catalog archetype.",
            "Prefer repeated items from dated/manual-confirmed builds over one-off direct-page scrape matches.",
            "Use records with empty items as placeholders for manual paste fallback.",
        ],
    }


def infer_notes(tag: str, sample_count: int, core: list[str], support: list[str]) -> str:
    if not core:
        return f"{tag}: not enough repeated item evidence yet; add manual paste records or fetch more samples."
    support_text = f" Support candidates: {', '.join(support[:8])}." if support else ""
    return f"{tag}: {sample_count} sample(s). Repeated core candidates: {', '.join(core[:8])}.{support_text}"


def record_to_dict(record: BuildRecord) -> dict:
    return {
        "url": record.url,
        "title": record.title,
        "date": record.date,
        "hero": record.hero,
        "tag": record.tag,
        "items": record.items,
        "source": record.source,
        "snippet": record.snippet,
        "fetch_status": record.fetch_status,
        "fetch_error": record.fetch_error,
    }


def _arch_lookup_key(name: str) -> str:
    """Canonical match key: strip win records + ' Build' suffix, casefold, alphanum only."""
    key = WIN_RECORD_RE.sub("", name.strip())
    key = re.sub(r"\s+[Bb]uild$", "", clean_space(key))
    return re.sub(r"[^a-z0-9]", "", key.casefold())


def _collect_arch_items(arch: dict) -> set[str]:
    items: set[str] = set()
    for field in ("condition_items", "core_items", "carry_items", "support_items"):
        for item in arch.get(field) or []:
            if item and not str(item).startswith("TODO"):
                items.add(str(item))
    return items


def compare_artifact_to_catalog(artifact: dict, catalog: dict) -> dict:
    """Compare artifact groups against hero build catalog archetypes.

    Returns a dict with keys: hero, updates, new_archetypes, noise.
    """
    hero = catalog.get("hero", artifact.get("filters", {}).get("hero") or "Unknown")
    hero_lower = hero.casefold()

    # Build catalog index: lookup_key -> (phase, archetype_dict)
    catalog_index: dict[str, tuple[str, dict]] = {}
    for phase, phase_data in (catalog.get("game_phases") or {}).items():
        if not isinstance(phase_data, dict):
            continue
        for arch in phase_data.get("archetypes") or []:
            if not isinstance(arch, dict):
                continue
            name = arch.get("name", "")
            if name:
                catalog_index[_arch_lookup_key(name)] = (phase, arch)

    updates = []
    new_archetypes = []
    noise = []

    for group in artifact.get("groups") or []:
        tag = group.get("tag", "")
        sample_count = group.get("sample_count", 0)
        candidate_core = group.get("candidate_core_items") or []
        candidate_support = group.get("candidate_support_items") or []
        freq_map = {
            row["item"]: row
            for row in (group.get("item_frequencies") or [])
            if isinstance(row, dict) and "item" in row
        }

        # Drop Uncategorized and vague generic group names
        tag_bare = re.sub(r"\s+[Bb]uild$", "", tag.strip()).strip().casefold()
        if tag_bare in ("uncategorized", "build", "") or any(p in tag.casefold() for p in GENERIC_TAG_PHRASES):
            noise.append({"group": tag, "reason": "generic or uncategorized"})
            continue

        # Drop cross-hero groups (title/tag resolves to a different known hero)
        inferred = infer_hero_from_url_or_title(None, tag)
        if inferred and inferred.casefold() != hero_lower:
            noise.append({"group": tag, "reason": f"cross-hero: {inferred}"})
            continue

        if not candidate_core and not candidate_support:
            noise.append({"group": tag, "reason": "no item evidence"})
            continue

        key = _arch_lookup_key(tag)
        match = catalog_index.get(key)

        if match:
            phase, arch = match
            existing_items = _collect_arch_items(arch)
            missing_core = [i for i in candidate_core if i not in existing_items]
            missing_support = [i for i in candidate_support if i not in existing_items]
            if missing_core or missing_support:
                updates.append({
                    "group": tag,
                    "phase": phase,
                    "archetype": arch.get("name", tag),
                    "sample_count": sample_count,
                    "missing_core": missing_core,
                    "missing_support": missing_support,
                    "freq_map": freq_map,
                })
        else:
            new_archetypes.append({
                "group": tag,
                "sample_count": sample_count,
                "candidate_core": candidate_core,
                "candidate_support": candidate_support,
                "freq_map": freq_map,
            })

    return {
        "hero": hero,
        "updates": updates,
        "new_archetypes": new_archetypes,
        "noise": noise,
    }


def _confidence_label(sample_count: int) -> str:
    if sample_count >= 3:
        return "high"
    if sample_count == 2:
        return "medium"
    return "low"


def _freq_note(item: str, sample_count: int, freq_map: dict) -> str:
    row = freq_map.get(item)
    if not row:
        return ""
    count = row.get("count", 0)
    return f" — seen {count}/{sample_count} builds"


def generate_proposal_markdown(result: dict) -> str:
    hero = result["hero"]
    updates = result["updates"]
    new_archetypes = result["new_archetypes"]
    noise = result["noise"]
    now = datetime.now().strftime("%Y-%m-%d")

    lines: list[str] = []
    lines.append(f"# {hero} Build Update Proposal")
    lines.append(f"\nGenerated: {now}")
    lines.append("\n> Auto-generated by `bazaar_build_enricher.py compare`. "
                 "Do not apply changes without human review.")

    lines.append("\n---\n")

    # ── Existing archetype updates ──────────────────────────────────────────
    lines.append("## Existing Archetype Updates\n")
    if not updates:
        lines.append("_No missing items found in matched archetypes._\n")
    for entry in updates:
        arch = entry["archetype"]
        phase = entry["phase"]
        sample_count = entry["sample_count"]
        freq_map = entry["freq_map"]
        lines.append(f"### {arch} ({phase})")
        lines.append(f"Evidence: {sample_count} sample(s), artifact group \"{entry['group']}\"\n")
        if entry["missing_core"]:
            lines.append("**Missing core candidates:**")
            for item in entry["missing_core"]:
                lines.append(f"- {item}{_freq_note(item, sample_count, freq_map)}")
            lines.append("")
        if entry["missing_support"]:
            lines.append("**Missing support candidates:**")
            for item in entry["missing_support"]:
                lines.append(f"- {item}{_freq_note(item, sample_count, freq_map)}")
            lines.append("")

    lines.append("---\n")

    # ── New archetype candidates ─────────────────────────────────────────────
    lines.append("## New Archetype Candidates\n")
    if not new_archetypes:
        lines.append("_No unmatched groups with item evidence._\n")
    for entry in new_archetypes:
        tag = entry["group"]
        sample_count = entry["sample_count"]
        confidence = _confidence_label(sample_count)
        lines.append(f"### {tag} — {confidence} confidence ({sample_count} sample(s))")
        lines.append("Suggested phase: late  _(verify before committing)_\n")
        if entry["candidate_core"]:
            lines.append(f"Candidate core items: {', '.join(entry['candidate_core'])}")
        if entry["candidate_support"]:
            lines.append(f"Candidate support items: {', '.join(entry['candidate_support'])}")
        lines.append("")

    lines.append("---\n")

    # ── Noise / no evidence ──────────────────────────────────────────────────
    lines.append("## Noise / No Evidence\n")
    if not noise:
        lines.append("_No noise groups._\n")
    for entry in noise:
        lines.append(f"- \"{entry['group']}\" — {entry['reason']}")

    return "\n".join(lines) + "\n"


def run_compare(args) -> None:
    artifact = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    result = compare_artifact_to_catalog(artifact, catalog)
    md = generate_proposal_markdown(result)
    print(md)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"Proposal written to {args.output}", file=sys.stderr)


def parse_since(args) -> Optional[date]:
    if args.since:
        parsed = parse_date(args.since)
        if not parsed:
            raise SystemExit(f"Could not parse --since date: {args.since}")
        return parsed
    if args.days:
        today = datetime.now().date()
        return date.fromordinal(today.toordinal() - args.days)
    return None


def run(args) -> dict:
    catalog_dir: Path = args.catalog_dir
    names_file: Path = args.names_file
    known_items = load_known_items(catalog_dir, names_file)
    since = parse_since(args)

    records: list[BuildRecord] = []
    html, status, error = fetch_url(args.category_url, args.timeout)
    if html:
        records.extend(extract_category_records(args.category_url, html, args.hero, args.limit))
        records.extend(extract_json_ld_records(html, args.category_url, args.hero, known_items))
    else:
        records.append(BuildRecord(
            url=args.category_url,
            hero=args.hero,
            tag="Uncategorized",
            source="category",
            fetch_status=status,
            fetch_error=error,
        ))

    manual_result = load_manual_records(args.manual, args.hero, known_items)
    records.extend(manual_result.records)
    records = dedupe_records(records)
    records = filter_records(records, since, hero=args.hero)

    if args.fetch_posts:
        records = [enrich_post(record, known_items, args.timeout) for record in records]

    summary = build_summary(
        records,
        category_url=args.category_url,
        hero=args.hero,
        since=since,
        days=args.days,
        known_items=known_items,
        warnings=manual_result.warnings,
    )
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch Bazaar Builds evidence and emit a human-review JSON artifact.",
    )
    parser.add_argument("category_url", help="Hero category URL, e.g. https://bazaar-builds.net/category/builds/dooley-builds/")
    parser.add_argument("--hero", help="Hero name to attach to records when it cannot be inferred.")
    parser.add_argument("--days", type=int, help="Keep records from the last N days. Undated records are kept.")
    parser.add_argument("--since", help="Keep records on or after this date. Overrides --days.")
    parser.add_argument("--limit", type=int, default=30, help="Maximum category/index links to collect.")
    parser.add_argument("--manual", action="append", default=[], help="Manual fallback JSON or text file. May be repeated.")
    parser.add_argument("--fetch-posts", action="store_true", help="Try direct individual post pages after collecting index records.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds.")
    parser.add_argument("--output", help="Write the artifact to this JSON file. Stdout is always printed.")
    parser.add_argument(
        "--catalog-dir",
        type=Path,
        default=_DEFAULT_TRACKER_ROOT,
        metavar="PATH",
        help=(
            "Directory containing *_builds.json catalog files. "
            f"Defaults to {_DEFAULT_TRACKER_ROOT} (sibling bazaar_tracker checkout)."
        ),
    )
    parser.add_argument(
        "--names-file",
        type=Path,
        default=_DEFAULT_TRACKER_ROOT / "card_cache_names.txt",
        metavar="PATH",
        help=(
            "Path to card_cache_names.txt generated by the tracker's refresh_images.py. "
            f"Defaults to {_DEFAULT_TRACKER_ROOT / 'card_cache_names.txt'}. "
            "Never vendor a copy — point at the live tracker checkout."
        ),
    )
    return parser


def build_compare_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare a build artifact against a hero catalog and emit a markdown proposal.",
        prog="bazaar_build_enricher.py compare",
    )
    parser.add_argument("artifact", help="Path to the artifact JSON (e.g. artifacts/dooley_bazaar_builds_summary.json)")
    parser.add_argument("catalog", help="Path to the hero builds JSON (e.g. dooley_builds.json)")
    parser.add_argument("--output", help="Write the markdown proposal to this file.")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args_list = list(argv) if argv is not None else sys.argv[1:]
    if args_list and args_list[0] == "compare":
        parser = build_compare_parser()
        args = parser.parse_args(args_list[1:])
        run_compare(args)
        return 0
    parser = build_arg_parser()
    args = parser.parse_args(args_list)
    summary = run(args)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
