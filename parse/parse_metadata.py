"""
Stage 2 - Parse exported TMDL (semantic models) and PBIR (reports) into one master metadata JSON.

Usage:
    python parse_metadata.py --raw ../output/raw --out ../output/metadata.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# ---------------------------------------------------------------- TMDL parsing

def _indent(line: str) -> int:
    return len(line) - len(line.lstrip("\t"))


def _split_header(line: str) -> tuple[str, str, str | None]:
    """'measure 'Total Calls' = EXPR' -> ('measure', 'Total Calls', 'EXPR')"""
    m = re.match(r"^(\w+)\s+('(?:[^']|'')+'|\"[^\"]+\"|[^\s=]+)\s*(?:=\s*(.*))?$", line.strip())
    if not m:
        return line.strip().split(" ", 1)[0], "", None
    kind, name, rest = m.group(1), m.group(2), m.group(3)
    return kind, name.strip("'\""), rest


def _block_text(lines: list[str], start: int, base_indent: int) -> tuple[str, int]:
    """Collect deeper-indented continuation lines (multi-line expressions)."""
    out, i = [], start
    while i < len(lines):
        ln = lines[i]
        if ln.strip() and _indent(ln) <= base_indent:
            break
        out.append(ln.strip())
        i += 1
    return "\n".join(out).strip(), i


def parse_table(path: Path) -> dict:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    table: dict = {"name": "", "columns": [], "measures": [], "partitions": [], "properties": {}}
    i = 0
    while i < len(lines):
        ln = lines[i]
        if not ln.strip() or ln.lstrip().startswith("///"):
            i += 1
            continue
        ind = _indent(ln)
        kind, name, rest = _split_header(ln)

        if ind == 0 and kind == "table":
            table["name"] = name
        elif ind == 1 and kind in {"measure", "calculatedColumn"}:
            expr = rest or ""
            j = i + 1
            if not expr:  # expression starts on the next, deeper-indented lines
                expr, j = _block_text(lines, i + 1, 1)
                # properties (formatString etc.) sit at indent 2 AFTER the expression;
                # _block_text swallowed them, so split them back out.
                expr_lines, props = [], {}
                for e in expr.split("\n"):
                    pm = re.match(r"^(formatString|displayFolder|description|isHidden|lineageTag):\s*(.*)$", e)
                    (props.__setitem__(pm.group(1), pm.group(2)) if pm else expr_lines.append(e))
                expr = "\n".join(expr_lines).strip()
            else:
                props = {}
                while j < len(lines) and lines[j].strip() and _indent(lines[j]) > 1:
                    pm = re.match(r"^\s*(\w+):\s*(.*)$", lines[j])
                    if pm:
                        props[pm.group(1)] = pm.group(2)
                    j += 1
            table["measures"].append({"name": name, "expression": expr, "format": props.get("formatString"),
                                      "displayFolder": props.get("displayFolder")})
            i = j
            continue
        elif ind == 1 and kind == "column":
            col = {"name": name, "dataType": None, "sourceColumn": None}
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or _indent(lines[j]) > 1):
                pm = re.match(r"^\s*(\w+):\s*(.*)$", lines[j])
                if pm and pm.group(1) in {"dataType", "sourceColumn"}:
                    col[pm.group(1)] = pm.group(2)
                j += 1
            table["columns"].append(col)
            i = j
            continue
        elif ind == 1 and kind == "partition":
            part = {"name": name, "type": (rest or "").strip(), "mode": None, "source": ""}
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or _indent(lines[j]) > 1):
                s = lines[j].strip()
                if s.startswith("mode:"):
                    part["mode"] = s.split(":", 1)[1].strip()
                elif s.startswith("source ="):
                    inline = s.split("=", 1)[1].strip()
                    if inline:
                        part["source"] = inline
                    else:
                        part["source"], j = _block_text(lines, j + 1, 2)
                        continue
                j += 1
            table["partitions"].append(part)
            i = j
            continue
        i += 1
    return table


def parse_relationships(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rels, cur = [], None
    for ln in path.read_text(encoding="utf-8-sig").splitlines():
        s = ln.strip()
        if s.startswith("relationship "):
            cur = {"from": None, "to": None}
            rels.append(cur)
        elif cur is not None and s.startswith("fromColumn:"):
            cur["from"] = s.split(":", 1)[1].strip()
        elif cur is not None and s.startswith("toColumn:"):
            cur["to"] = s.split(":", 1)[1].strip()
    return rels


# --------------------------------------------------- data source extraction (M)

_SNOWFLAKE = re.compile(r'Snowflake\.Databases\(\s*"([^"]+)"')
_SQL = re.compile(r'Sql\.Database\(\s*"([^"]+)"\s*,\s*"([^"]+)"')
_NAV = re.compile(r'\{\s*\[\s*Name\s*=\s*"([^"]+)"\s*,\s*Kind\s*=\s*"(Database|Schema|Table|View)"')
_EXCEL = re.compile(r'Excel\.Workbook\(\s*File\.Contents\(\s*"([^"]+)"')
_SP = re.compile(r'SharePoint\.(?:Files|Contents|Tables)\(\s*"([^"]+)"')


def extract_sources(m_code: str) -> list[dict]:
    out = []
    if (sf := _SNOWFLAKE.search(m_code)):
        nav = {kind: name for name, kind in _NAV.findall(m_code)}
        out.append({"type": "Snowflake", "server": sf.group(1), **{k.lower(): v for k, v in nav.items()}})
    if (sq := _SQL.search(m_code)):
        out.append({"type": "SQL Server", "server": sq.group(1), "database": sq.group(2)})
    if (xl := _EXCEL.search(m_code)):
        out.append({"type": "Excel", "path": Path(xl.group(1)).name})  # filename only - avoid leaking paths
    if (sp := _SP.search(m_code)):
        out.append({"type": "SharePoint", "site": sp.group(1)})
    return out


# ------------------------------------------------------------------ PBIR parsing

def _field_ref(field: dict) -> dict | None:
    for kind in ("Column", "Measure"):
        if kind in field:
            node = field[kind]
            entity = node.get("Expression", {}).get("SourceRef", {}).get("Entity")
            return {"kind": kind.lower(), "table": entity, "name": node.get("Property")}
    return None


def parse_report(report_dir: Path) -> dict:
    defn = report_dir / "definition"
    pages_meta = json.loads((defn / "pages" / "pages.json").read_text(encoding="utf-8"))
    pages = []
    for page_id in pages_meta.get("pageOrder", []):
        pdir = defn / "pages" / page_id
        pj = json.loads((pdir / "page.json").read_text(encoding="utf-8"))
        visuals = []
        for vfile in sorted((pdir / "visuals").glob("*/visual.json")):
            vj = json.loads(vfile.read_text(encoding="utf-8"))
            v = vj.get("visual", {})
            fields = []
            for role, spec in v.get("query", {}).get("queryState", {}).items():
                for proj in spec.get("projections", []):
                    if (ref := _field_ref(proj.get("field", {}))):
                        fields.append({"role": role, **ref})
            visuals.append({"id": vj.get("name"), "type": v.get("visualType"), "fields": fields})
        pages.append({"id": page_id, "name": pj.get("displayName", page_id), "visuals": visuals})
    return {"pages": pages}


# ------------------------------------------------------------------ data quality

def lint_model(model: dict) -> list[str]:
    """Flag measures that reference columns/measures that don't exist in the model."""
    cols = {(t["name"], c["name"]) for t in model["tables"] for c in t["columns"]}
    measures = {m["name"] for t in model["tables"] for m in t["measures"]}
    tables = {t["name"] for t in model["tables"]}
    issues = []
    for t in model["tables"]:
        for m in t["measures"]:
            for tbl, col in re.findall(r"'?([A-Za-z_][\w ]*?)'?\[([^\]]+)\]", m["expression"]):
                tbl = tbl.strip()
                if tbl in tables and (tbl, col) not in cols and col not in measures:
                    issues.append(f"{t['name']}[{m['name']}] references missing {tbl}[{col}]")
    return sorted(set(issues))


# ------------------------------------------------------------------ orchestration

def build(raw_root: Path) -> dict:
    result = {"workspaces": []}
    for ws_dir in sorted(p for p in raw_root.iterdir() if p.is_dir()):
        ws = {"name": ws_dir.name, "models": {}, "reports": []}
        for model_dir in sorted(ws_dir.glob("*.SemanticModel")):
            defn = model_dir / "definition"
            tables = [parse_table(p) for p in sorted((defn / "tables").glob("*.tmdl"))]
            for t in tables:
                srcs = []
                for part in t["partitions"]:
                    srcs += extract_sources(part["source"])
                t["sources"] = srcs
            model = {"name": model_dir.name.removesuffix(".SemanticModel"),
                     "tables": tables, "relationships": parse_relationships(defn / "relationships.tmdl")}
            model["lint"] = lint_model(model)
            ws["models"][model["name"]] = model

        for rep_dir in sorted(ws_dir.glob("*.Report")):
            rep = parse_report(rep_dir)
            rep["name"] = rep_dir.name.removesuffix(".Report")
            pbir = rep_dir / "definition.pbir"
            rep["model"] = None
            if pbir.exists():
                ref = json.loads(pbir.read_text(encoding="utf-8")).get("datasetReference", {})
                path = ref.get("byPath", {}).get("path", "")
                rep["model"] = Path(path).name.removesuffix(".SemanticModel") or None
            ws["reports"].append(rep)
        result["workspaces"].append(ws)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    data = build(args.raw)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    n_models = sum(len(w["models"]) for w in data["workspaces"])
    n_reports = sum(len(w["reports"]) for w in data["workspaces"])
    print(f"Parsed {n_models} model(s), {n_reports} report(s) -> {args.out}")


if __name__ == "__main__":
    main()
