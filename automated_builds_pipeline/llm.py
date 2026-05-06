"""LLM classification with known-item validation and confidence surfacing.

High-confidence classifications are emitted as top-line proposals, medium
classifications are carried as weaker signals, and low classifications are
suppressed by the normal diff generator path. Unknown item names are coerced to
``invalid`` so hallucinated output never reaches catalog proposal buckets.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

DEFAULT_MODEL = "claude-sonnet-4-6"
PROMPT_DIR = Path(__file__).resolve().parents[1] / "llm_prompts"
VALID_CLASSIFICATIONS = {"carry", "core", "support", "invalid"}
VALID_CONFIDENCES = {"high", "medium", "low"}


@dataclass(frozen=True)
class ItemClassification:
    item: str
    classification: str
    confidence: str
    rationale: str
    surface: str

    @classmethod
    def from_dict(cls, data: dict[str, Any], known_items: set[str]) -> "ItemClassification":
        item = str(data.get("item", "")).strip()
        classification = str(data.get("classification", "invalid")).strip().lower()
        confidence = str(data.get("confidence", "low")).strip().lower()
        rationale = str(data.get("rationale", "")).strip()
        if classification not in VALID_CLASSIFICATIONS:
            classification = "invalid"
        if confidence not in VALID_CONFIDENCES:
            confidence = "low"
        if item not in known_items:
            classification = "invalid"
            rationale = rationale or "Unknown item name returned by LLM."
        return cls(
            item=item,
            classification=classification,
            confidence=confidence,
            rationale=rationale,
            surface=_surface_for(classification, confidence),
        )

    def to_diff_dict(self) -> dict[str, str]:
        return {
            "item": self.item,
            "llm_classification": self.classification,
            "llm_rationale": self.rationale,
            "llm_confidence": self.confidence,
            "surface": self.surface,
        }


class LLMClassifier:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        prompt_version: str = "v1",
        known_items_path: Path = Path("card_cache_names.txt"),
        *,
        api_key_env: str = "CLAUDE_API_KEY",
        client: Any = None,
    ) -> None:
        self.model = model
        self.prompt_version = prompt_version
        self.prompt_template = _load_prompt(prompt_version)
        self.known_items = load_known_items(known_items_path)
        self._client = client
        self._api_key_env = api_key_env

    def classify_archetype(
        self,
        hero: str,
        phase: Optional[str],
        archetype: Optional[str],
        existing_buckets: dict[str, list[str]],
        candidate_items: list[dict[str, Any]],
        evidence_summary: dict[str, Any],
        mobalytics_description: Optional[str],
    ) -> list[ItemClassification]:
        prompt = self._assemble_prompt(
            hero,
            phase,
            archetype,
            existing_buckets,
            candidate_items,
            evidence_summary,
            mobalytics_description,
        )
        payload = self._call_with_retries(prompt)
        return [ItemClassification.from_dict(item, self.known_items) for item in payload]

    def _assemble_prompt(
        self,
        hero: str,
        phase: Optional[str],
        archetype: Optional[str],
        existing_buckets: dict[str, list[str]],
        candidate_items: list[dict[str, Any]],
        evidence_summary: dict[str, Any],
        mobalytics_description: Optional[str],
    ) -> str:
        body = {
            "hero": hero,
            "phase": phase,
            "archetype": archetype,
            "existing_catalog_state": existing_buckets,
            "candidate_items": candidate_items,
            "evidence_summary": evidence_summary,
            "mobalytics_description": mobalytics_description or "",
            "known_items": sorted(self.known_items),
        }
        return json.dumps(body, indent=2, sort_keys=True)

    def _call_with_retries(self, prompt: str) -> list[dict[str, Any]]:
        last_error: Optional[BaseException] = None
        for attempt in range(3):
            try:
                response = self._client_or_create().messages.create(
                    model=self.model,
                    temperature=0,
                    max_tokens=2048,
                    system=[
                        {
                            "type": "text",
                            "text": self.prompt_template,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": prompt}],
                    tools=[_classification_tool()],
                    tool_choice={"type": "tool", "name": "classify_items"},
                )
                return _extract_tool_payload(response)
            except Exception as exc:  # pragma: no cover - exercised with mocked SDK shapes
                last_error = exc
                if attempt >= 2 or not _is_transient(exc):
                    raise
                time.sleep(2**attempt)
        if last_error:
            raise last_error
        return []

    def _client_or_create(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("anthropic package is required for LLM classification") from exc
        api_key = os.environ.get(self._api_key_env)
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} must be set for LLM classification")
        self._client = anthropic.Anthropic(api_key=api_key)
        return self._client


def load_known_items(known_items_path: Path, catalog: Optional[dict[str, Any]] = None) -> set[str]:
    names: set[str] = set()
    if known_items_path.exists():
        names.update(line.strip() for line in known_items_path.read_text(encoding="utf-8").splitlines() if line.strip())
    if catalog:
        names.update(_catalog_item_names(catalog))
    return names


def validate_classifications(items: list[dict[str, Any]], known_items: set[str]) -> list[ItemClassification]:
    return [ItemClassification.from_dict(item, known_items) for item in items]


def _load_prompt(prompt_version: str) -> str:
    path = PROMPT_DIR / f"classifier_{prompt_version}.txt"
    if not path.exists():
        raise FileNotFoundError(f"LLM prompt artifact not found: {path}")
    return path.read_text(encoding="utf-8")


def _classification_tool() -> dict[str, Any]:
    return {
        "name": "classify_items",
        "description": "Classify Bazaar build items for one hero archetype.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "item": {"type": "string"},
                            "classification": {"type": "string", "enum": sorted(VALID_CLASSIFICATIONS)},
                            "confidence": {"type": "string", "enum": sorted(VALID_CONFIDENCES)},
                            "rationale": {"type": "string"},
                        },
                        "required": ["item", "classification", "confidence", "rationale"],
                    },
                }
            },
            "required": ["items"],
        },
    }


def _extract_tool_payload(response: Any) -> list[dict[str, Any]]:
    content = response.get("content", []) if isinstance(response, dict) else getattr(response, "content", [])
    for block in content:
        block_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
        name = getattr(block, "name", None) or (block.get("name") if isinstance(block, dict) else None)
        if block_type == "tool_use" and name == "classify_items":
            payload = getattr(block, "input", None) or block.get("input", {})
            items = payload.get("items", [])
            if not isinstance(items, list):
                raise ValueError("LLM tool payload items must be a list")
            return [item for item in items if isinstance(item, dict)]
    raise ValueError("LLM response did not include classify_items tool output")


def _is_transient(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status in {408, 409, 429, 500, 502, 503, 504}:
        return True
    name = exc.__class__.__name__.lower()
    return any(token in name for token in ("rate", "timeout", "connection", "overload"))


def _surface_for(classification: str, confidence: str) -> str:
    if classification == "invalid" or confidence == "low":
        return "suppressed"
    if confidence == "medium":
        return "weaker_signal"
    return "top_line"


def _catalog_item_names(catalog: dict[str, Any]) -> set[str]:
    names: set[str] = set()

    game_phases = catalog.get("game_phases")
    if isinstance(game_phases, dict):
        for phase_data in game_phases.values():
            if not isinstance(phase_data, dict):
                continue
            for archetype in phase_data.get("archetypes", []) or []:
                if not isinstance(archetype, dict):
                    continue
                for bucket in ("carry_items", "core_items", "support_items", "condition_items"):
                    raw_bucket = archetype.get(bucket, [])
                    if isinstance(raw_bucket, list):
                        names.update(str(item) for item in raw_bucket if item)
            for bucket in ("universal_utility_items", "economy_items"):
                raw_bucket = phase_data.get(bucket, [])
                if isinstance(raw_bucket, list):
                    names.update(str(item) for item in raw_bucket if item)

    raw_items = catalog.get("items", [])
    if isinstance(raw_items, list):
        for item in raw_items:
            if isinstance(item, dict) and isinstance(item.get("item"), str):
                names.add(item["item"])
    for entry in catalog.get("archetypes", []) or []:
        if not isinstance(entry, dict):
            continue
        for bucket in ("carry_items", "core_items", "support_items", "condition_items"):
            raw_bucket = entry.get(bucket, [])
            if isinstance(raw_bucket, list):
                names.update(str(item) for item in raw_bucket)
    return names
