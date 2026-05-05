#!/usr/bin/env python3
"""
extract_bazaar_bundle_pngs_v2.py

Exports Sprite / Texture2D images from one Bazaar Unity cache bundle.

Install once:
    py -m pip install UnityPy pillow

Run:
    py extract_bazaar_bundle_pngs_v2.py
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from pathlib import Path
from typing import Optional

DEFAULT_BUNDLE = (
    r"C:\Users\Matt\AppData\LocalLow\Unity\Tempo Storm_The Bazaar"
    r"\1da6bc7e26414a6732f91f89588f2946"
    r"\b1214a7a5eb7c28a4094d56974612130"
    r"\__data"
)

DEFAULT_CACHE_ROOT = (
    r"C:\Users\Matt\AppData\LocalLow\Unity\Tempo Storm_The Bazaar"
)

UNITY_VERSION = "6000.3.11f1"


def _normalize_card_name(value: str) -> str:
    """Lowercase + strip non-alphanumerics. Used to build manifest keys."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def safe_filename(value: str, fallback: str) -> str:
    value = (value or "").strip() or fallback
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:180] or fallback


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    for i in range(2, 10000):
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Could not generate unique filename for {path}")


def classify_image(width: int, height: int) -> str:
    if width == 1024 and height == 1024:
        return "likely_card_art"
    if width >= 512 and height >= 512:
        return "large_art"
    if width <= 128 and height <= 128:
        return "small_icon"
    if height > width * 1.5:
        return "portrait_or_tall_art"
    return "misc"


def export_image(image, out_dir: Path, filename_base: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = unique_path(out_dir / f"{filename_base}.png")
    image.save(path)
    return path


def write_manifests(out_dir: Path, rows: list[dict]) -> None:
    json_path = out_dir / "manifest.json"
    csv_path = out_dir / "manifest.csv"

    json_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    if rows:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    print("Manifest JSON:", json_path)
    print("Manifest CSV: ", csv_path)


def _process_bundle_cards_only(
    bundle_path: Path,
    out_dir: Path,
    by_card_key: dict[str, dict],
    counts: dict[str, int],
    *,
    min_width: int,
    min_height: int,
    cache_root: Optional[Path] = None,
    UnityPy_module=None,
) -> bool:
    """Export /Cards/*_D.png textures from one bundle into ``out_dir``.

    Mutates ``by_card_key`` and ``counts`` in place. Returns True if the bundle
    parsed and ran to completion, False if UnityPy.load failed.

    On collision (same normalized card key already in ``by_card_key``), keeps
    whichever entry has the higher pixel count; original wins on tie.
    """
    try:
        env = UnityPy_module.load(str(bundle_path))
    except Exception as exc:
        print(f"[ScanAll] failed to load {bundle_path}: {exc}")
        counts["bundle_errors"] = counts.get("bundle_errors", 0) + 1
        return False

    container_by_path_id: dict[int, str] = {}
    for key, obj_ref in env.container.items():
        try:
            if obj_ref.type.name == "Texture2D":
                container_by_path_id[obj_ref.path_id] = key
        except Exception:
            continue

    # Bundle path recorded in manifest is relative to cache_root when possible,
    # so the manifest stays portable across machines that share the same root.
    if cache_root is not None:
        try:
            bundle_record = str(bundle_path.relative_to(cache_root))
        except ValueError:
            bundle_record = str(bundle_path)
    else:
        bundle_record = str(bundle_path)

    for obj in env.objects:
        try:
            if obj.type.name != "Texture2D":
                continue
        except Exception:
            continue

        container_key = container_by_path_id.get(obj.path_id, "")
        if "/Cards/" not in container_key or not container_key.endswith("_D.png"):
            continue

        image_file = container_key.rsplit("/", 1)[-1]
        card_folder = container_key.rsplit("/", 2)[-2]

        try:
            data = obj.read()
            image = getattr(data, "image", None)
            if image is None:
                counts["skipped"] += 1
                continue
            width, height = image.width, image.height
            if width < min_width or height < min_height:
                counts["skipped"] += 1
                continue

            normalized = _normalize_card_name(card_folder)
            existing = by_card_key.get(normalized)

            if existing is not None:
                # Highest-resolution wins, with first-wins as the tiebreaker.
                existing_pixels = (existing.get("width") or 0) * (existing.get("height") or 0)
                new_pixels = width * height
                if new_pixels <= existing_pixels:
                    print(
                        f"[ScanAll] collision: {normalized!r} - keeping "
                        f"{existing['image_file']} ({existing.get('width')}x{existing.get('height')}), "
                        f"ignoring {image_file} ({width}x{height})"
                    )
                    counts["collisions"] = counts.get("collisions", 0) + 1
                    continue
                # New entry wins. Remove the old PNG so we don't leave orphans.
                old_path = out_dir / existing["image_file"]
                if old_path.is_file():
                    try:
                        old_path.unlink()
                    except OSError:
                        pass
                print(
                    f"[ScanAll] collision: {normalized!r} - replacing "
                    f"{existing['image_file']} ({existing.get('width')}x{existing.get('height')}) "
                    f"with {image_file} ({width}x{height})"
                )
                counts["collisions"] = counts.get("collisions", 0) + 1

            out_dir.mkdir(parents=True, exist_ok=True)
            path = unique_path(out_dir / image_file)
            image.save(path)
            counts["Texture2D"] += 1

            by_card_key[normalized] = {
                "card_folder": card_folder,
                "image_file": path.name,
                "container_key": container_key,
                "texture_path_id": obj.path_id,
                "width": width,
                "height": height,
                "bundle": bundle_record,
            }

            print(f"EXPORTED  {width:4d}x{height:<4d}  {path.name}")
        except Exception as e:
            counts["errors"] += 1
            print(f"Skipped Texture2D path_id={getattr(obj, 'path_id', '?')}: {e}")

    return True


def main() -> int:
    try:
        import UnityPy
        UnityPy.config.FALLBACK_UNITY_VERSION = UNITY_VERSION
    except ImportError:
        print("Missing UnityPy.")
        print("Run: py -m pip install UnityPy pillow")
        return 2

    desktop = Path(os.environ["USERPROFILE"]) / "Desktop"
    script_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", default=DEFAULT_BUNDLE)
    parser.add_argument("--out", default=None)
    parser.add_argument("--min-width", type=int, default=16)
    parser.add_argument("--min-height", type=int, default=16)
    parser.add_argument(
        "--cards-only",
        action="store_true",
        help="Export only /Cards/*_D.png Texture2D entries to a flat dir with by_card_key manifest.",
    )
    parser.add_argument(
        "--scan-all",
        action="store_true",
        help="Walk the cache root for every Unity asset bundle (__data and *.bundle). Requires --cards-only.",
    )
    parser.add_argument(
        "--cache-root",
        default=DEFAULT_CACHE_ROOT,
        help=f"Cache root for --scan-all (default: {DEFAULT_CACHE_ROOT}).",
    )
    args = parser.parse_args()

    if args.scan_all and not args.cards_only:
        print("[ScanAll] --scan-all requires --cards-only.")
        return 2

    if args.out is None:
        if args.cards_only:
            default_out = script_dir / "static_cache" / "images"
        else:
            default_out = desktop / "bazaar_card_art_test_v2"
        args.out = str(default_out)

    bundle_path = Path(args.bundle)
    out_dir = Path(args.out)
    sprites_dir = out_dir / "sprites"
    textures_dir = out_dir / "textures"

    out_dir.mkdir(parents=True, exist_ok=True)

    if args.scan_all:
        cache_root = Path(args.cache_root)
        if not cache_root.is_dir():
            print(f"[ScanAll] cache root not found: {cache_root}")
            return 2

        print(f"[ScanAll] scanning cache root: {cache_root}")
        # Find both Unity bundle naming conventions:
        # - "__data" files: used in the user-data Addressables cache
        #   (%USERPROFILE%\AppData\LocalLow\...)
        # - "*.bundle" files: used in the Steam install's StreamingAssets
        #   (e.g. <install>\TheBazaar_Data\StreamingAssets\aa\StandaloneWindows64\)
        # UnityPy.load() handles both formats identically.
        bundle_paths = sorted(
            {
                p
                for p in (
                    *cache_root.rglob("__data"),
                    *cache_root.rglob("*.bundle"),
                )
                if p.is_file()
            }
        )
        print(f"[ScanAll] found {len(bundle_paths)} bundle file(s)")

        # Clear any stale PNGs / manifest from a prior run so we don't leave
        # orphans behind. unique_path() guards against in-run collisions but
        # not against pre-existing files.
        cleared = 0
        for stale in out_dir.glob("*.png"):
            try:
                stale.unlink()
                cleared += 1
            except OSError:
                pass
        stale_manifest = out_dir / "manifest.json"
        if stale_manifest.is_file():
            try:
                stale_manifest.unlink()
            except OSError:
                pass
        if cleared:
            print(f"[ScanAll] cleared {cleared} stale PNG(s) from prior run")

        by_card_key: dict[str, dict] = {}
        counts = {
            "Sprite": 0, "Texture2D": 0, "skipped": 0, "errors": 0,
            "bundle_errors": 0, "collisions": 0,
        }
        bundles_ok = 0

        for i, bp in enumerate(bundle_paths, start=1):
            print(f"\n[ScanAll] [{i}/{len(bundle_paths)}] {bp}")
            ok = _process_bundle_cards_only(
                bp, out_dir, by_card_key, counts,
                min_width=args.min_width, min_height=args.min_height,
                cache_root=cache_root, UnityPy_module=UnityPy,
            )
            if ok:
                bundles_ok += 1

        manifest_path = out_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps({"by_card_key": by_card_key}, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        print()
        print(f"[ScanAll] bundles scanned:    {bundles_ok}/{len(bundle_paths)}")
        print(f"[ScanAll] bundle load errors: {counts['bundle_errors']}")
        print(f"[ScanAll] textures exported:  {counts['Texture2D']}")
        print(f"[ScanAll] manifest entries:   {len(by_card_key)}")
        print(f"[ScanAll] collisions:         {counts['collisions']}")
        print(f"[ScanAll] skipped (filters):  {counts['skipped']}")
        print(f"[ScanAll] per-object errors:  {counts['errors']}")
        print(f"[ScanAll] manifest:           {manifest_path}")
        print(f"[ScanAll] output:             {out_dir}")
        return 0

    print("Loading:", bundle_path)
    env = UnityPy.load(str(bundle_path))
    print("\n=== CONTAINER ENTRIES ===")
    for key, obj in env.container.items():
        try:
            print(key, "=>", obj.type.name, obj.path_id)
        except Exception as e:
            print(key, "=>", e)

    container_by_path_id: dict[int, str] = {}
    if args.cards_only:
        for key, obj_ref in env.container.items():
            try:
                if obj_ref.type.name == "Texture2D":
                    container_by_path_id[obj_ref.path_id] = key
            except Exception:
                continue

    manifest: list[dict] = []
    by_card_key: dict[str, dict] = {}
    counts = {"Sprite": 0, "Texture2D": 0, "skipped": 0, "errors": 0}

    for obj in env.objects:
        type_name = obj.type.name

        if args.cards_only:
            if type_name != "Texture2D":
                continue
            container_key = container_by_path_id.get(obj.path_id, "")
            if "/Cards/" not in container_key or not container_key.endswith("_D.png"):
                continue

            image_file = container_key.rsplit("/", 1)[-1]
            card_folder = container_key.rsplit("/", 2)[-2]

            try:
                data = obj.read()
                image = getattr(data, "image", None)
                if image is None:
                    counts["skipped"] += 1
                    continue
                width, height = image.width, image.height
                if width < args.min_width or height < args.min_height:
                    counts["skipped"] += 1
                    continue

                out_dir.mkdir(parents=True, exist_ok=True)
                path = unique_path(out_dir / image_file)
                image.save(path)
                counts["Texture2D"] += 1

                normalized = _normalize_card_name(card_folder)
                if normalized in by_card_key:
                    existing = by_card_key[normalized]
                    print(
                        f"[CardsOnly] collision: {normalized!r} - keeping "
                        f"{existing['image_file']}, ignoring {image_file}"
                    )
                else:
                    by_card_key[normalized] = {
                        "card_folder": card_folder,
                        "image_file": path.name,
                        "container_key": container_key,
                        "texture_path_id": obj.path_id,
                        "width": width,
                        "height": height,
                    }

                print(f"EXPORTED  {width:4d}x{height:<4d}  {path.name}")
            except Exception as e:
                counts["errors"] += 1
                print(f"Skipped Texture2D path_id={getattr(obj, 'path_id', '?')}: {e}")
            continue

        if type_name not in ("Sprite", "Texture2D"):
            continue

        try:
            data = obj.read()
            name = getattr(data, "name", "") or f"{type_name}_{obj.path_id}"
            image = getattr(data, "image", None)

            if image is None:
                counts["skipped"] += 1
                continue

            width, height = image.width, image.height
            if width < args.min_width or height < args.min_height:
                counts["skipped"] += 1
                continue

            classification = classify_image(width, height)

            filename_base = safe_filename(name, f"{type_name}_{obj.path_id}")
            target_dir = sprites_dir if type_name == "Sprite" else textures_dir
            path = export_image(image, target_dir, filename_base)

            counts[type_name] += 1

            row = {
                "png_file": path.name,
                "relative_path": str(path.relative_to(out_dir)),
                "asset_name": name,
                "type": type_name,
                "path_id": obj.path_id,
                "width": width,
                "height": height,
                "classification": classification,
                "bundle": str(bundle_path),
            }
            manifest.append(row)

            print(
                f"EXPORTED {type_name:9s} "
                f"{width:4d}x{height:<4d} "
                f"{classification:18s} "
                f"{path.name}"
            )

        except Exception as e:
            counts["errors"] += 1
            print(f"Skipped {type_name} path_id={getattr(obj, 'path_id', '?')}: {e}")

    if args.cards_only:
        manifest_path = out_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps({"by_card_key": by_card_key}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print()
        print(f"[CardsOnly] exported {len(by_card_key)} card images")
        print(f"[CardsOnly] manifest: {manifest_path}")
        print(f"[CardsOnly] output:   {out_dir}")
    else:
        write_manifests(out_dir, manifest)

        print()
        print("Done.")
        print("Sprites exported: ", counts["Sprite"])
        print("Textures exported:", counts["Texture2D"])
        print("Skipped:          ", counts["skipped"])
        print("Errors:           ", counts["errors"])
        print("Output:           ", out_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())