"""
Summarise a metadata.json produced by parse_metadata.py.

Use it to generate real, defensible numbers for the README or a post:
    python stats.py --metadata ../output/metadata.json
"""
import argparse
import json
from collections import Counter
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--metadata", required=True, type=Path)
    args = ap.parse_args()
    data = json.loads(args.metadata.read_text(encoding="utf-8"))

    workspaces = data["workspaces"]
    models = [m for w in workspaces for m in w["models"].values()]
    reports = [r for w in workspaces for r in w["reports"]]
    tables = [t for m in models for t in m["tables"]]
    measures = [x for t in tables for x in t["measures"]]
    visuals = [v for r in reports for p in r["pages"] for v in p["visuals"]]
    sources = Counter(s["type"] for t in tables for s in t.get("sources", []))
    lint = [i for m in models for i in m["lint"]]

    print(f"Workspaces:        {len(workspaces)}")
    print(f"Semantic models:   {len(models)}")
    print(f"Reports:           {len(reports)}")
    print(f"Pages:             {sum(len(r['pages']) for r in reports)}")
    print(f"Visuals:           {len(visuals)}")
    print(f"Tables:            {len(tables)}")
    print(f"DAX measures:      {len(measures)}")
    print(f"Relationships:     {sum(len(m['relationships']) for m in models)}")
    print(f"Data sources:      {dict(sources) or 'none detected'}")
    print(f"Lint findings:     {len(lint)}")
    for issue in lint:
        print(f"  - {issue}")


if __name__ == "__main__":
    main()
