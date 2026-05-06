# McMaster Navigator MCP

Unofficial headless MCP server for navigating McMaster-Carr and finding exact part numbers from text descriptions or page navigation.

This server is for the gap before your product-data pipeline: it finds McMaster part numbers from rendered search/category pages. Once you have part numbers, use your own product API or CAD/data-sheet pipeline for detailed assets.

This project is not affiliated with, endorsed by, or sponsored by McMaster-Carr.

## Features

- Headless-only browsing with SeleniumBase UC mode.
- Stateful navigation across searches, categories, product pages, back, and current page.
- Product number extraction from rendered links, images, and page HTML.
- Dynamic schema extraction for rendered product tables: columns, row attributes, group headers, filters, and part-number rows.
- Exact-part resolver that searches multiple query roots, extracts live rendered table schemas, maps constraints onto dynamic fields, and returns a part number only when matching rows collapse to one candidate.
- GPT-backed schema matching with deterministic row filtering: the model proposes fields and accepted live values, while Python performs the actual filtering.
- Dynamic handling for linked option variants, accessory rows, prefixed table columns, dimensions, materials, selected options, model numbers, and packaging.
- MCP-safe stdio transport; browser logs are kept off protocol stdout.
- Isolated temporary Chrome profile by default so stale user sessions do not break agent runs.
- Automatic one-time browser reset/retry for common first-run ChromeDriver failures.
- Sequential browser session with Selenium page-load timeouts.
- Browser-free URL helper for fast product/search URL generation.

## Requirements

- Python 3.11 or newer
- Google Chrome installed
- Network access to `https://www.mcmaster.com`

SeleniumBase downloads a matching driver automatically on first use. The first live request can take longer than later requests.

## Install

```bash
pip install mcmaster-navigator-mcp
```

You can also send someone a built wheel:

```bash
pip install /path/to/mcmaster_navigator_mcp-0.5.0-py3-none-any.whl
```

For local development:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## MCP Config

After PyPI publish, the lowest-friction config is:

```json
{
  "mcpServers": {
    "mcmaster-navigator": {
      "command": "uvx",
      "args": ["mcmaster-navigator-mcp"]
    }
  }
}
```

If you install it into an environment yourself:

```json
{
  "mcpServers": {
    "mcmaster-navigator": {
      "command": "mcmaster-navigator-mcp"
    }
  }
}
```

For a local virtual environment, point the MCP client at the installed console script:

```json
{
  "mcpServers": {
    "mcmaster-navigator": {
      "command": "/absolute/path/to/.venv/bin/mcmaster-navigator-mcp"
    }
  }
}
```

## Tools

- `mcmaster_find_exact_part`: best default when the user supplied enough detail to identify one catalog item. By default uses `strategy: dynamic_schema`, which requires `OPENAI_API_KEY`; returns `status: unique`, `ambiguous`, `unresolved`, `budget_exceeded`, or `error`.
- `mcmaster_find_parts`: broad search. Searches and browses rendered pages, then returns part numbers.
- `mcmaster_search`: search McMaster and return the rendered page state.
- `mcmaster_open`: open a URL, path, part number, or search phrase.
- `mcmaster_extract_schema`: open/search a page and return dynamically extracted filters, table columns, row attributes, and part-number rows.
- `mcmaster_follow_link`: follow a link from the current page by index, text, or URL.
- `mcmaster_current_page`: inspect the current rendered page.
- `mcmaster_back`: go back in browser history.
- `mcmaster_url`: generate URLs without launching a browser.
- `mcmaster_doctor`: return environment diagnostics.
- `mcmaster_close_browser`: close/reset the headless browser worker.

## Example Query

Ask your agent:

```text
Use mcmaster_find_exact_part to find the part number for an 18-8 stainless steel socket head screw, M14 x 2 mm thread, 25 mm long, pack of 5.
```

Expected output shape:

```json
{
  "description": "18-8 stainless steel socket head screw, M14 x 2 mm thread, 25 mm long, pack of 5",
  "part_number": "90696A101",
  "selected_part": {
    "part_number": "90696A101",
    "name": "18-8 Stainless Steel Socket Head Screw, M14 x 2 mm Thread, 25 mm Long",
    "score": 0.99
  },
  "candidates": [
    {
      "part_number": "90696A101",
      "evidence": "Family: Stainless Steel Socket Head Screws; Group: 18-8 Stainless Steel; Group: M14 x 2 mm; Lg.: 25 mm"
    }
  ],
  "pages_visited": []
}
```

## Dynamic Page Schemas

`mcmaster_extract_schema` exposes the live option schema found on rendered McMaster pages instead of assuming a fixed ontology for screws, springs, switches, fittings, and other part families.

Example output shape:

```json
{
  "title": "Stainless Steel Socket Head Screws | McMaster-Carr",
  "schemas": [
    {
      "family_title": "Stainless Steel Socket Head Screws",
      "filters": [
        {"text": "M14 x 2 mm", "url": "https://www.mcmaster.com/..."}
      ],
      "tables": [
        {
          "title": "Stainless Steel Socket Head Screws",
          "columns": ["Lg.", "Pkg. Qty."],
          "rows": [
            {
              "part_number": "90696A101",
              "family": "Stainless Steel Socket Head Screws",
              "groups": ["18-8 Stainless Steel", "M14 x 2 mm"],
              "attributes": {
                "Lg.": "25 mm",
                "Pkg. Qty.": "5"
              }
            }
          ]
        }
      ]
    }
  ]
}
```

Agents can use this as a dynamic catalog interface: first discover the fields available on the current product-family page, then match normalized component constraints against the extracted row attributes.

## Dynamic Exact Resolution

`mcmaster_find_exact_part` defaults to `strategy: dynamic_schema`. The flow is:

1. Use GPT to propose broad McMaster search roots and literal constraints from the description.
2. Headlessly search all root queries, then spend remaining page budget on ranked live links.
3. Extract dynamic table rows, group headings, option links, and attributes from rendered pages.
4. Use GPT to map requested constraints to the live fields and exact live values.
5. Deterministically filter rows, grounding explicit `Family:`, `Group:`, selected-option, model-number, and attribute labels against the live schema. If the first mapping conflicts with the live schema, GPT repairs the mapping from the extracted field/value schema.

The resolver returns one part only when the filtered live rows have one unique part number. If the text is under-specified, it returns multiple candidates as `ambiguous`; if the page schema cannot support the requested constraints, it returns `unresolved`.

To force the older text-ranker:

```json
{"description": "...", "strategy": "deterministic"}
```

## Environment Variables

- `MCMASTER_NAV_PROFILE_DIR`: optional persistent Chrome profile directory. Default: a temporary isolated profile per server process.
- `MCMASTER_NAV_PAGE_TIMEOUT`: Selenium page load timeout in seconds. Default: `45`.
- `MCMASTER_NAV_SETTLE_SECONDS`: render settle delay after navigation. Default: `3`.
- `MCMASTER_NAV_AUTO_DRILL_DEPTH`: category levels to auto-open during search. Default: `2`.
- `MCMASTER_NAV_MAX_PRODUCTS`: maximum products extracted from one page. Default: `80`.
- `MCMASTER_NAV_MAX_LINKS`: maximum links extracted from one page. Default: `100`.
- `MCMASTER_NAV_TOOL_TIMEOUT`: MCP tool timeout in seconds. Default: `300`.
- `OPENAI_API_KEY`: required for `mcmaster_find_exact_part` with `strategy: dynamic_schema`.
- `MCMASTER_NAV_LLM_MODEL`: model for dynamic schema matching. Default falls back to `FUSION_LLM_MODEL`, then `gpt-5.4-mini`.
- `MCMASTER_NAV_LLM_TOKEN_BUDGET`: per-tool-call token cap for the dynamic resolver. Default: `2500000`.
- `MCMASTER_NAV_LLM_MAX_SEARCHES`: maximum search roots generated by the dynamic resolver. Default: `2`.
- `MCMASTER_NAV_LLM_MAX_ROWS`: maximum extracted rows sent through dynamic schema matching. Default: `700`.
- `MCMASTER_NAV_LLM_MAX_FIELD_VALUES`: maximum sample values per field sent to the model. Default: `160`.

## Benchmark

The live benchmark starts from known part numbers, writes a part description without the part number, and checks whether `mcmaster_find_exact_part` returns the original single part number.

Latest deterministic local run:

```text
benchmark_runs/exact_part_100_v2
100/100 top-1 exact part recovery
mean lookup time: 94.992 seconds
max_pages: 20
reuse_browser: false
```

Latest dynamic schema gate:

```text
benchmark_runs/llm_schema_250_general2
250/250 unique exact part recovery
250/250 top-1 exact part recovery
250/250 returned exactly one part
mean lookup time: 71.038 seconds
median lookup time: 54.448 seconds
p95 lookup time: 161.763 seconds
max lookup time: 466.125 seconds
categories: 25
model: gpt-5.4-mini
LLM usage recorded in artifacts: 1,862,490 tokens across 250 cases
max_pages: 8
llm_max_searches: 2
llm_max_rows: 700
llm_max_field_values: 160
case_timeout_seconds: 600
```

The benchmark intentionally includes details needed to identify one row or product option, such as material, model number, dimensions, selected option, package/each choice, and compatible manufacturer models.

## Publishing

Build locally:

```bash
python -m build
twine check dist/*
```

Publish:

```bash
twine upload dist/*
```

Use a package name and project description that clearly identify this as an unofficial navigator MCP.

## Validation

This package should pass the local checks before publishing:

```bash
python -m pytest -q
python -m build
twine check dist/*
```

For a live smoke test, run the server through an MCP client and call `mcmaster_find_parts` with a query such as `brass ball valve`.

For exact-part validation:

```bash
MCMASTER_NAV_SETTLE_SECONDS=2 python benchmarks/mcmaster_retrieval_benchmark.py \
  --target 100 --per-query 8 --max-results 20 --max-pages 20 --auto-drill-depth 2
```

For dynamic schema validation with GPT:

```bash
MCMASTER_NAV_SETTLE_SECONDS=2 python benchmarks/mcmaster_retrieval_benchmark.py \
  --selector llm-schema --target 10 --max-pages 8 --auto-drill-depth 2 \
  --llm-env-file /path/to/.env --llm-token-budget 240000 \
  --case-timeout-seconds 600 --case-timeout-retries 1
```

To regenerate paper-style metrics from any benchmark run directory:

```bash
python benchmarks/analyze_run.py benchmark_runs/llm_schema_100_final7
```
