"""
Stage 3 - Turn parsed metadata into plain-English dashboard documentation with Claude.

For every report it builds a compact fact sheet (pages, visuals, measures used, data sources,
model lint findings) and asks Claude for a structured description: purpose, audience, KPIs,
per-page summaries, data lineage. Output: descriptions.json + a browsable DASHBOARD_CATALOG.md.

Usage:
    export ANTHROPIC_API_KEY=...            # not needed with --dry-run
    python generate_descriptions.py --metadata ../output/metadata.json --out ../output
    python generate_descriptions.py --metadata ../output/metadata.json --out ../output --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

SYSTEM = (
    "You document Power BI dashboards for a catalog read by business and technical stakeholders. "
    "Use ONLY the facts provided. Never invent KPIs, data sources or business context that is not "
    "in the fact sheet; if purpose is uncertain, say so in `confidence_notes`. "
    "Reply with a single JSON object and nothing else."
)

SCHEMA_HINT = """{
  "purpose": "1-2 sentences on what decision/question this dashboard supports",
  "audience": "likely primary users",
  "kpis": [{"name": "...", "definition": "plain-English meaning of the measure"}],
  "pages": [{"name": "...", "summary": "1 sentence"}],
  "data_sources": ["..."],
  "confidence_notes": "what was inferred vs stated; mention any model lint issues"
}"""


def fact_sheet(ws: dict, report: dict) -> dict:
    model = ws["models"].get(report.get("model") or "", {})
    measures = {m["name"]: m for t in model.get("tables", []) for m in t["measures"]}

    pages, used = [], {}
    for p in report["pages"]:
        vis = []
        for v in p["visuals"]:
            vis.append({"type": v["type"], "fields": [f'{f["table"]}[{f["name"]}]' for f in v["fields"]]})
            for f in v["fields"]:
                if f["kind"] == "measure" and f["name"] in measures:
                    used[f["name"]] = measures[f["name"]]["expression"]
        pages.append({"name": p["name"], "visuals": vis})

    sources = []
    for t in model.get("tables", []):
        for s in t.get("sources", []):
            if s not in sources:
                sources.append(s)

    return {
        "report": report["name"],
        "workspace_label": ws["name"],
        "pages": pages,
        "measures_used": used,
        "data_sources": sources,
        "tables": [t["name"] for t in model.get("tables", [])],
        "relationships": model.get("relationships", []),
        "model_lint_issues": model.get("lint", []),
    }


def describe_with_claude(facts: dict) -> dict:
    import anthropic  # imported lazily so --dry-run works without the SDK

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Fact sheet:\n{json.dumps(facts, indent=2)}\n\nReturn JSON matching:\n{SCHEMA_HINT}",
        }],
    )
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    text = text.removeprefix("```json").removesuffix("```").strip()
    return json.loads(text)


def describe_dry_run(facts: dict) -> dict:
    """Deterministic stand-in so the pipeline can be demoed without an API key."""
    return {
        "purpose": f"(dry run) Report '{facts['report']}' with {len(facts['pages'])} page(s).",
        "audience": "(dry run)",
        "kpis": [{"name": n, "definition": e.splitlines()[0][:120]} for n, e in facts["measures_used"].items()],
        "pages": [{"name": p["name"], "summary": f"{len(p['visuals'])} visual(s)"} for p in facts["pages"]],
        "data_sources": [json.dumps(s) for s in facts["data_sources"]],
        "confidence_notes": "(dry run) No model call made. Lint: " + ("; ".join(facts["model_lint_issues"]) or "none"),
    }


def to_markdown(entries: list[dict]) -> str:
    out = ["# Dashboard Catalog", "", f"_{len(entries)} dashboard(s), auto-generated from TMDL/PBIR definitions._", ""]
    for e in entries:
        d = e["description"]
        out += [f"## {e['report']}", f"*Workspace:* {e['workspace']}  ", f"**Purpose:** {d['purpose']}  ",
                f"**Audience:** {d['audience']}", "", "**KPIs**"]
        out += [f"- **{k['name']}** - {k['definition']}" for k in d["kpis"]] or ["- (none detected)"]
        out += ["", "**Pages**"] + [f"- **{p['name']}** - {p['summary']}" for p in d["pages"]]
        out += ["", "**Data sources**"] + [f"- {s}" for s in d["data_sources"]]
        out += ["", f"> {d['confidence_notes']}", ""]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--metadata", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    data = json.loads(args.metadata.read_text(encoding="utf-8"))
    entries = []
    for ws in data["workspaces"]:
        for rep in ws["reports"]:
            facts = fact_sheet(ws, rep)
            desc = describe_dry_run(facts) if args.dry_run else describe_with_claude(facts)
            entries.append({"workspace": ws["name"], "report": rep["name"], "facts": facts, "description": desc})
            print(f"described: {ws['name']} / {rep['name']}")

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "descriptions.json").write_text(json.dumps(entries, indent=2), encoding="utf-8")
    (args.out / "DASHBOARD_CATALOG.md").write_text(to_markdown(entries), encoding="utf-8")
    print(f"Wrote {args.out / 'DASHBOARD_CATALOG.md'}")


if __name__ == "__main__":
    main()
