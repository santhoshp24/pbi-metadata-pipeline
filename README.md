# Power BI Dashboard Metadata Pipeline

Automatically document every Power BI dashboard in a Microsoft Fabric / Premium estate: what each one is for, which KPIs it shows, which pages it has, and where its data comes from. It reads the dashboards' own definitions (TMDL and PBIR) and uses Claude to turn them into plain-English catalog entries.

> **Demo data only.** Everything in `sample_data/` is a synthetic port-operations model created for this repo. No employer or client data is included.

## Why this exists

Large BI estates accumulate dozens of dashboards across workspaces. Nobody knows what half of them do, which sources they hit, or what breaks when a source moves. Documenting them by hand takes weeks and is out of date the day it's finished. This pipeline makes the inventory repeatable: re-run it whenever you need a fresh catalog.

## Architecture

```
Fabric REST API            PowerShell            Python                 Claude API
(Get Item Definition) ---> 1. Extract  ------->  2. Parse   ---------> 3. Describe ---> DASHBOARD_CATALOG.md
 TMDL + PBIR per item      raw definition        metadata.json          purpose, KPIs,      descriptions.json
                           files on disk         (tables, measures,     pages, lineage
                                                 relationships,
                                                 sources, visuals,
                                                 lint findings)
```

| Stage | File | What it does |
|---|---|---|
| 1. Extract | `extract/Export-FabricDefinitions.ps1` | Signs in as **you** (delegated auth, no service principal), lists workspaces and items, calls Get Item Definition (handles the 202 long-running-operation pattern and pagination), decodes parts to disk, writes a manifest of successes and failures. |
| 2. Parse | `parse/parse_metadata.py` | Parses TMDL (tables, columns, measures incl. multi-line DAX, partitions) and PBIR (pages, visuals, field bindings) into one JSON. Extracts data sources from Power Query M (Snowflake, SQL Server, Excel, SharePoint). Lints measures for references to columns that don't exist. |
| 3. Describe | `describe/generate_descriptions.py` | Builds a compact fact sheet per report and asks Claude for a structured description. The prompt restricts Claude to the facts supplied and requires a `confidence_notes` field, so inference is flagged rather than hidden. |

## Design decisions

- **Delegated user auth, not a service principal.** It only ever sees what the signed-in user can already access, which is simpler to get approved and has a smaller blast radius.
- **Three stages with files between them.** Each stage can be re-run or debugged on its own. Parsing is deterministic and free; only stage 3 costs tokens, and it only sees a distilled fact sheet rather than raw files.
- **LLM only for the part that needs language.** Structure (tables, measures, lineage) is parsed with code. Claude writes the human-readable "what is this for" layer.
- **Grounded generation.** Claude is told to use only the fact sheet and to separate stated from inferred facts. Model lint issues are passed in so documentation gaps surface in the catalog.
- **Privacy by default.** Excel sources record the file name only, not the full path; `.gitignore` blocks committing real exports.

## Run it on the demo data (no Fabric access needed)

```bash
pip install -r requirements.txt

python parse/parse_metadata.py --raw sample_data/raw --out output/metadata.json

# without an API key (deterministic placeholder text):
python describe/generate_descriptions.py --metadata output/metadata.json --out output --dry-run

# with Claude:
export ANTHROPIC_API_KEY=...
python describe/generate_descriptions.py --metadata output/metadata.json --out output
```

See `sample_data/expected_output/` for the parser output on the demo model.

## Run it on a real estate

```powershell
Install-Module Az.Accounts -Scope CurrentUser
./extract/Export-FabricDefinitions.ps1 -WorkspaceNames "My-Workspace" -OutDir ./output/raw
```

```bash
python parse/parse_metadata.py --raw output/raw --out output/metadata.json
python describe/generate_descriptions.py --metadata output/metadata.json --out output
```

Requirements: workspaces on Premium or Fabric capacity, and permission to read item definitions in each workspace.

## Limitations

- The TMDL parser covers the constructs needed for documentation (tables, columns, measures, partitions, relationships). It is not a full TMDL grammar: calculation groups, perspectives and roles are not parsed yet.
- Reports must be in a format that exposes PBIR-style page and visual JSON. Older PBIX-style report definitions need a different parser.
- The measure-reference lint is regex-based and can miss unusual syntax.
- Descriptions are drafts. A human should review the `confidence_notes` before publishing a catalog.

## Possible next steps

- Publish the catalog as a Power BI report or a searchable site
- Diff catalogs between runs to detect new, changed and retired dashboards
- Add usage metrics so the catalog highlights unused dashboards
- Parse calculation groups and RLS roles
- Build a column-level lineage view (source table to measure to visual)

## Results

_Add your real numbers here before posting, for example: number of dashboards documented, hours saved versus manual documentation, number of workspaces covered, issues found by the lint._

---
Author: Santosh Pothula
