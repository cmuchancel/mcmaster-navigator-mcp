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
- GPT-backed schema matching with programmatic row filtering: the model proposes fields and accepted live values, while Python performs the actual filtering.
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

For exact-part resolution, provide an OpenAI API key with `OPENAI_API_KEY`, `--openai-api-key-file`, or `--openai-api-key`. No other OpenAI or model configuration is required.

## Install

```bash
pip install mcmaster-navigator-mcp
```

You can also send someone a built wheel:

```bash
pip install /path/to/mcmaster_navigator_mcp-0.5.4-py3-none-any.whl
```

For local development:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## MCP Config

For MCP clients, the lowest-friction config is:

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

Do not commit API keys into MCP config files. Prefer exporting `OPENAI_API_KEY` in your shell or using your MCP client's secret-management mechanism.

### API Key Configuration

The normal package setup is to launch the MCP server with `OPENAI_API_KEY` in its environment:

```bash
export OPENAI_API_KEY="YOUR_OPENAI_API_KEY"
mcmaster-navigator-mcp
```

For MCP clients that can pass environment variables directly:

```json
{
  "mcpServers": {
    "mcmaster-navigator": {
      "command": "uvx",
      "args": ["mcmaster-navigator-mcp"],
      "env": {
        "OPENAI_API_KEY": "YOUR_OPENAI_API_KEY"
      }
    }
  }
}
```

If you prefer keeping the secret in a local file outside the repo, pass the file path:

```json
{
  "mcpServers": {
    "mcmaster-navigator": {
      "command": "uvx",
      "args": [
        "mcmaster-navigator-mcp",
        "--openai-api-key-file",
        "/absolute/path/to/openai_api_key.txt"
      ]
    }
  }
}
```

Direct key passing is also supported for clients that inject command arguments securely:

```json
{
  "mcpServers": {
    "mcmaster-navigator": {
      "command": "mcmaster-navigator-mcp",
      "args": ["--openai-api-key", "YOUR_OPENAI_API_KEY"]
    }
  }
}
```

Prefer `OPENAI_API_KEY` or `--openai-api-key-file` on shared machines because command-line arguments can be visible to process-list tools.

## Tools

- `mcmaster_find_exact_part`: best default when the user supplied enough detail to identify one catalog item. Uses the dynamic schema resolver, requires an OpenAI API key, and returns `status: unique`, `ambiguous`, `unresolved`, or `error`.
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
  "status": "unique",
  "part_number": "90696A101",
  "selected_part": {
    "part_number": "90696A101",
    "family": "Stainless Steel Socket Head Screws",
    "groups": ["18-8 Stainless Steel", "M14 x 2 mm"],
    "attributes": {
      "Lg.": "25 mm",
      "Pkg. Qty.": "5"
    }
  },
  "candidates": [
    {
      "part_number": "90696A101",
      "family": "Stainless Steel Socket Head Screws",
      "groups": ["18-8 Stainless Steel", "M14 x 2 mm"],
      "attributes": {
        "Lg.": "25 mm",
        "Pkg. Qty.": "5"
      }
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

`mcmaster_find_exact_part` uses dynamic schema resolution. The flow is:

1. Use GPT to propose broad McMaster search roots and literal constraints from the description.
2. Headlessly search all root queries, then spend remaining page budget on candidate live links.
3. Extract dynamic table rows, group headings, option links, and attributes from rendered pages.
4. Use GPT to map requested constraints to the live fields and exact live values.
5. Programmatically filter rows, grounding explicit `Family:`, `Group:`, selected-option, model-number, and attribute labels against the live schema. If the first mapping conflicts with the live schema, GPT repairs the mapping from the extracted field/value schema.

The resolver returns one part only when the filtered live rows have one unique part number. If zero rows match, it returns `unresolved`. If multiple rows still match, it returns the matching part numbers as `ambiguous`.

## Environment Variables

- `OPENAI_API_KEY`: OpenAI API key for `mcmaster_find_exact_part`. Required unless `--openai-api-key-file` or `--openai-api-key` is provided.

Optional browser/runtime tuning:

- `MCMASTER_NAV_PROFILE_DIR`: optional persistent Chrome profile directory. Default: a temporary isolated profile per server process.
- `MCMASTER_NAV_PAGE_TIMEOUT`: Selenium page load timeout in seconds. Default: `45`.
- `MCMASTER_NAV_SETTLE_SECONDS`: render settle delay after navigation. Default: `3`.
- `MCMASTER_NAV_AUTO_DRILL_DEPTH`: category levels to auto-open during search. Default: `2`.
- `MCMASTER_NAV_MAX_PRODUCTS`: maximum products extracted from one page. Default: `80`.
- `MCMASTER_NAV_MAX_LINKS`: maximum links extracted from one page. Default: `100`.
- `MCMASTER_NAV_TOOL_TIMEOUT`: MCP tool timeout in seconds. Default: `300`.

## Paper Benchmarks

The benchmark scripts stay in `benchmarks/` for paper runs, but they are excluded from the PyPI source distribution. Results are written under ignored `benchmark_runs/` directories.

Exact recovery:

```bash
export OPENAI_API_KEY="..."
MCMASTER_NAV_SETTLE_SECONDS=2 python benchmarks/mcmaster_retrieval_benchmark.py \
  --selector llm-schema --target 250 --max-pages 8 --auto-drill-depth 2 \
  --llm-token-budget 2500000
```

Nonexistent and broad ambiguity checks:

```bash
export OPENAI_API_KEY="..."
python benchmarks/negative_ambiguity_benchmark.py \
  --source-run benchmark_runs/llm_schema_250_general2 \
  --target-per-kind 25 --kinds nonexistent,ambiguous
```

Near-ambiguity checks:

```bash
export OPENAI_API_KEY="..."
python benchmarks/near_ambiguity_benchmark.py \
  --source-run benchmark_runs/llm_schema_250_general2 \
  --target 25
```

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
