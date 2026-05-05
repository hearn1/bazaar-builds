"""
Probe a single card bundle from the Steam install to find out HOW card art
is stored. Our existing filter (/Cards/*_D.png) only found 13 cards across
2012 bundles, which means the bulk of card art uses a different convention.

This script picks one card bundle (default: card_jules) and reports:
  1. Total object counts by type.
  2. All Texture2D objects with their container paths and dimensions.
  3. All Sprite objects with their container paths and which texture they reference.
  4. A sample of unique container path patterns (first 30).

Run:
    python probe_install_card_bundle.py
    python probe_install_card_bundle.py --bundle-substring card_jules
    python probe_install_card_bundle.py --bundle-substring card_common
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

import UnityPy
import UnityPy.config

UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.11f1"

DEFAULT_INSTALL_ROOT = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\The Bazaar"
    r"\TheBazaar_Data\StreamingAssets\aa\StandaloneWindows64"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--install-root",
        type=Path,
        default=DEFAULT_INSTALL_ROOT,
        help="Path to StandaloneWindows64 dir.",
    )
    parser.add_argument(
        "--bundle-substring",
        default="card_jules",
        help="Substring to find which bundle to probe (default: card_jules).",
    )
    args = parser.parse_args()

    if not args.install_root.is_dir():
        print(f"Install root not found: {args.install_root}")
        return 2

    matches = sorted(
        p for p in args.install_root.glob(f"*{args.bundle_substring}*.bundle")
    )
    if not matches:
        print(f"No bundles matched substring {args.bundle_substring!r}.")
        return 2
    bundle_path = matches[0]
    print(f"Probing: {bundle_path.name}")
    print(f"Size:    {bundle_path.stat().st_size / 1_000_000:.1f} MB")
    print()

    env = UnityPy.load(str(bundle_path))

    # Index container so we can map path_id -> container path
    container_by_path_id: dict[int, str] = {}
    for container_path, obj in env.container.items():
        container_by_path_id[obj.path_id] = container_path

    type_counts: Counter[str] = Counter()
    texture2d_entries = []
    sprite_entries = []
    container_path_samples = []

    for obj in env.objects:
        type_name = obj.type.name
        type_counts[type_name] += 1

        container_path = container_by_path_id.get(obj.path_id)

        if type_name == "Texture2D":
            try:
                data = obj.read()
                texture2d_entries.append(
                    {
                        "name": getattr(data, "m_Name", None) or getattr(data, "name", None) or "",
                        "container": container_path,
                        "width": getattr(data, "m_Width", None),
                        "height": getattr(data, "m_Height", None),
                        "path_id": obj.path_id,
                    }
                )
            except Exception as exc:
                texture2d_entries.append(
                    {
                        "name": f"<read error: {exc}>",
                        "container": container_path,
                        "width": None,
                        "height": None,
                        "path_id": obj.path_id,
                    }
                )

        elif type_name == "Sprite":
            try:
                data = obj.read()
                # The Sprite -> Texture2D reference is in m_RD.texture (PPtr)
                tex_path_id = None
                rd = getattr(data, "m_RD", None)
                if rd is not None:
                    tex_ref = getattr(rd, "texture", None)
                    if tex_ref is not None:
                        tex_path_id = getattr(tex_ref, "path_id", None) or getattr(
                            tex_ref, "m_PathID", None
                        )
                sprite_entries.append(
                    {
                        "name": getattr(data, "m_Name", None) or getattr(data, "name", None) or "",
                        "container": container_path,
                        "tex_path_id": tex_path_id,
                        "path_id": obj.path_id,
                    }
                )
            except Exception as exc:
                sprite_entries.append(
                    {
                        "name": f"<read error: {exc}>",
                        "container": container_path,
                        "tex_path_id": None,
                        "path_id": obj.path_id,
                    }
                )

        if container_path and len(container_path_samples) < 30:
            container_path_samples.append(container_path)

    print("=" * 72)
    print("OBJECT TYPE COUNTS")
    print("=" * 72)
    for type_name, count in type_counts.most_common():
        print(f"  {type_name:30s}  {count:6d}")

    print()
    print("=" * 72)
    print(f"TEXTURE2D OBJECTS  ({len(texture2d_entries)} total)")
    print("=" * 72)
    # Sort largest-first; that's where atlases will jump out.
    sorted_textures = sorted(
        texture2d_entries,
        key=lambda e: (e.get("width") or 0) * (e.get("height") or 0),
        reverse=True,
    )
    for entry in sorted_textures[:40]:
        w, h = entry["width"], entry["height"]
        size_str = f"{w}x{h}" if w and h else "?x?"
        container = entry["container"] or "<no container>"
        print(f"  {size_str:>10s}  pid={entry['path_id']:<22d}  {entry['name']!r}")
        print(f"             container: {container}")
    if len(sorted_textures) > 40:
        print(f"  ... ({len(sorted_textures) - 40} more textures not shown)")

    print()
    print("=" * 72)
    print(f"SPRITE OBJECTS  ({len(sprite_entries)} total)")
    print("=" * 72)
    if sprite_entries:
        # Group sprites by which texture they reference
        sprites_by_tex: dict[int | None, list[dict]] = defaultdict(list)
        for entry in sprite_entries:
            sprites_by_tex[entry["tex_path_id"]].append(entry)

        print(f"  Sprites reference {len(sprites_by_tex)} distinct texture(s).")
        print()
        for tex_pid, sprites in sorted(
            sprites_by_tex.items(),
            key=lambda kv: -len(kv[1]),
        )[:8]:
            tex_match = next(
                (t for t in texture2d_entries if t["path_id"] == tex_pid), None
            )
            tex_desc = "<unknown texture>"
            if tex_match:
                tex_desc = (
                    f"{tex_match['name']!r} "
                    f"({tex_match['width']}x{tex_match['height']})"
                )
            print(f"  Texture {tex_pid}: {tex_desc}")
            print(f"  -> {len(sprites)} sprite(s) reference this texture")
            for sprite in sprites[:5]:
                container = sprite["container"] or "<no container>"
                print(f"     * {sprite['name']!r}")
                print(f"       container: {container}")
            if len(sprites) > 5:
                print(f"     ... ({len(sprites) - 5} more)")
            print()
    else:
        print("  (no Sprite objects in this bundle)")

    print("=" * 72)
    print("SAMPLE CONTAINER PATHS (first 30)")
    print("=" * 72)
    for cp in container_path_samples:
        print(f"  {cp}")

    return 0


if __name__ == "__main__":
    sys.exit(main())