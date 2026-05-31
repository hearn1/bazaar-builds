import json, pathlib, sys

for p in sorted(pathlib.Path("artifacts/verify").glob("*_diff.json")):
    d = json.load(open(p, encoding="utf-8"))
    pc = d["proposed_changes"]
    health = {s["source"]: s["status"] for s in d["source_health"]}
    print(
        f"{p.stem}: bazaardb={health.get('bazaardb','?')} "
        f"adds={len(pc['archetype_additions'])} "
        f"updates={len(pc['archetype_updates'])} "
        f"arch_removes={len(pc['archetype_removal_candidates'])} "
        f"item_removes={len(pc['item_removal_candidates'])}"
    )
    for a in pc["archetype_additions"]:
        core = [i["item"] for i in a.get("candidate_core", [])]
        support = [i["item"] for i in a.get("candidate_support", [])]
        print(
            f"  ADD {a.get('candidate_phase')} / {a.get('tag')}: "
            f"core={core} support={support}"
        )
    for u in pc["archetype_updates"]:
        items = [(i.get("item"), i.get("llm_classification")) for i in u.get("missing_items", [])]
        print(f"  UPDATE {u.get('phase')} / {u.get('archetype')}: {items}")
