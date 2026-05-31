import json, pathlib
from automated_builds_pipeline.applier import apply_proposed_changes

for p in sorted(pathlib.Path("artifacts/verify").glob("*_diff.json")):
    hero = p.stem.split("_")[0]
    diff = json.load(open(p, encoding="utf-8"))
    catalog_path = pathlib.Path(f"../bazaar_tracker/builds/{hero.lower()}_builds.json")
    catalog = json.load(open(catalog_path, encoding="utf-8"))
    result = apply_proposed_changes(catalog, diff)
    print(f"{hero}: applied={len(result.applied)} skipped={len(result.skipped)} changed={result.changed}")
    for s in result.skipped:
        print(f"  SKIP {s}")
    for a in result.applied:
        print(f"  APPLY {a}")
