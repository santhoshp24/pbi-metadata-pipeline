"""
Stage 3 - Turn parsed metadata into plain-English documentation with Claude.

Builds a compact fact sheet for every report AND every semantic model, then asks Claude for a
structured description (purpose, audience, KPIs, pages or tables, data lineage).
Output: descriptions.json + a browsable DASHBOARD_CATALOG.md.

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
MAX_COLUMNS_PER_TABLE = 40  # keep fact sheets compact

SYSTEM = (
    "You document Power BI dashboards and semantic models for a catalog read by business and "
    "technical stakeholders. Use ONLY the facts provided. Never invent KPIs, data sources or "
    "business context that is not in the fact sheet; if purpose is uncertain, say so in "
    "`confidence_notes`. Reply with a single JSON object and nothing else."
)

REPORT_SCHEMA = """{
  "purpose": "1-2 sentences on what decision/question this dashboard supports",
  "audience": "likely primary users",
  "kpis": [{"name": "...", "definition": "plain-English meaning of the measure"}],
  "pages": [{"name": "...", "summary": "1 sentence"}],
  "data_sources": ["..."],
  "confidence_notes": "what was inferred vs stated; mention any model lint issues"
}"""

MODEL_SCHEMA = """{
  "purpose": "1-2 sentences on the business subject this semantic model covers",
  "audience": "likely primary users",
  "kpis": [{"name": "...", "definition": "plain-English meaning of the measure"}],
  "tables": [{"name": "...", "role": "fact | dimension | other", "summary": "1 sentence"}],
  "data_sources": ["..."],
  "confidence_notes": "what was inferred vs stated; mention any model lint issues"
}"""


# ----------------------------------------------------------------------------- fact sheets

def _model_sources(model: dict) -> list[dict]:
    sources = []
    for t in model.get("tables", []):
        for s in t.get("sources", []):
            if s not in sources:
                sources.append(s)
    return sources


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

    return {
        "report": report["name"],
        "workspace_label": ws["name"],
        "pages": pages,
        "measures_used": used,
        "data_sources": _model_sources(model),
        "tables": [t["name"] for t in model.get("tables", [])],
        "relationships": model.get("relationships", []),
        "model_lint_issues": model.get("lint", []),
    }


def model_fact_sheet(ws: dict, model: dict) -> dict:
    tables = []
    for t in model["tables"]:
        tables.append({
            "name": t["name"],
            "columns": [f'{c["name"]} ({c["dataType"]})' for c in t["columns"][:MAX_COLUMNS_PER_TABLE]],
            "columns_omitted": max(0, len(t["columns"]) - MAX_COLUMNS_PER_TABLE),
            "measures": {m["name"]: m["expression"] for m in t["measures"]},
            "sources": t.get("sources", []),
        })
    reports = [r["name"] for r in ws["reports"] if r.get("model") == model["name"]]
    return {
        "semantic_model": model["name"],
        "workspace_label": ws["name"],
        "tables": tables,
        "relationships": model["relationships"],
        "reports_using_this_model": reports,
        "model_lint_issues": model.get("lint", []),
    }


# ----------------------------------------------------------------------------- Claude calls

def describe_with_claude(facts: dict, schema: str) -> dict:
    import anthropic  # imported lazily so --dry-run works without the SDK

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Fact sheet:\n{json.dumps(facts, indent=2)}\n\nReturn JSON matching:\n{schema}",
        }],
    )
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    text = text.removeprefix("```json").removesuffix("```").strip()
    return json.loads(text)


def describe_dry_run(facts: dict, kind: str) -> dict:
    """Deterministic stand-in so the pipeline can be demoed without an API key."""
    lint = "; ".join(facts["model_lint_issues"]) or "none"
    if kind == "report":
        return {
            "purpose": f"(dry run) Report '{facts['report']}' with {len(facts['pages'])} page(s).",
            "audience": "(dry run)",
            "kpis": [{"name": n, "definition": e.splitlines()[0][:120]} for n, e in facts["measures_used"].items()],
            "pages": [{"name": p["name"], "summary": f"{len(p['visuals'])} visual(s)"} for p in facts["pages"]],
            "data_sources": [json.dumps(s) for s in facts["data_sources"]],
            "confidence_notes": "(dry run) No model call made. Lint: " + lint,
        }
    sources = []
    for t in facts["tables"]:
        for s in t["sources"]:
            if json.dumps(s) not in sources:
                sources.append(json.dumps(s))
    return {
        "purpose": f"(dry run) Semantic model '{facts['semantic_model']}' with {len(facts['tables'])} table(s).",
        "audience": "(dry run)",
        "kpis": [{"name": n, "definition": e.splitlines()[0][:120]}
                 for t in facts["tables"] for n, e in t["measures"].items()],
        "tables": [{"name": t["name"], "role": "other", "summary": f"{len(t['columns'])} column(s)"}
                   for t in facts["tables"]],
        "data_sources": sources,
        "confidence_notes": "(dry run) No model call made. Lint: " + lint,
    }


# ----------------------------------------------------------------------------- output

def _src_label(s) -> str:
    return s if isinstance(s, str) else json.dumps(s)


def to_markdown(entries: list[dict]) -> str:
    n_rep = sum(e["kind"] == "report" for e in entries)
    n_mod = len(entries) - n_rep
    out = ["# Dashboard Catalog", "",
           f"_{n_rep} report(s) and {n_mod} semantic model(s), auto-generated from TMDL/PBIR definitions._", ""]
    for kind, heading in (("report", "Reports"), ("semantic_model", "Semantic models")):
        group = [e for e in entries if e["kind"] == kind]
        if not group:
            continue
        out += [f"# {heading}", ""]
        for e in group:
            d = e["description"]
            out += [f"## {e['name']}", f"*Workspace:* {e['workspace']}  ", f"**Purpose:** {d['purpose']}  ",
                    f"**Audience:** {d['audience']}", "", "**KPIs**"]
            out += [f"- **{k['name']}** - {k['definition']}" for k in d["kpis"]] or ["- (none detected)"]
            if kind == "report":
                out += ["", "**Pages**"] + [f"- **{p['name']}** - {p['summary']}" for p in d["pages"]]
            else:
                out += ["", "**Tables**"] + [f"- **{t['name']}** ({t['role']}) - {t['summary']}" for t in d["tables"]]
            out += ["", "**Data sources**"] + [f"- {_src_label(s)}" for s in d["data_sources"]]
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
            desc = describe_dry_run(facts, "report") if args.dry_run else describe_with_claude(facts, REPORT_SCHEMA)
            entries.append({"kind": "report", "workspace": ws["name"], "name": rep["name"],
                            "facts": facts, "description": desc})
            print(f"described report: {ws['name']} / {rep['name']}")
        for model in ws["models"].values():
            facts = model_fact_sheet(ws, model)
            desc = describe_dry_run(facts, "model") if args.dry_run else describe_with_claude(facts, MODEL_SCHEMA)
            entries.append({"kind": "semantic_model", "workspace": ws["name"], "name": model["name"],
                            "facts": facts, "description": desc})
            print(f"described model:  {ws['name']} / {model['name']}")

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "descriptions.json").write_text(json.dumps(entries, indent=2), encoding="utf-8")
    (args.out / "DASHBOARD_CATALOG.md").write_text(to_markdown(entries), encoding="utf-8")
    print(f"Wrote {args.out / 'DASHBOARD_CATALOG.md'}")


if __name__ == "__main__":
    main()
