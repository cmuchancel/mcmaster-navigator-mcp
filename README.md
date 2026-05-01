# McMaster Navigator MCP

Unofficial headless MCP server for navigating McMaster-Carr and finding exact part numbers from text descriptions or page navigation.

This server is for the gap before your product-data pipeline: it finds McMaster part numbers from rendered search/category pages. Once you have part numbers, use your own product API or CAD/data-sheet pipeline for detailed assets.

This project is not affiliated with, endorsed by, or sponsored by McMaster-Carr.

## Features

- Headless-only browsing with SeleniumBase UC mode.
- Stateful navigation across searches, categories, product pages, back, and current page.
- Product number extraction from rendered links, images, and page HTML.
- Exact-part resolver that searches multiple query variants, reads rendered table rows, opens candidate product pages, and returns one best part number.
- Row-aware scoring for dimensions, materials, selected options, model numbers, packaging, and accessory/product-type distinctions.
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
pip install /path/to/mcmaster_navigator_mcp-0.2.0-py3-none-any.whl
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

- `mcmaster_find_exact_part`: best default when the user supplied enough detail to identify one catalog item. Returns a single selected part number plus ranked evidence.
- `mcmaster_find_parts`: broad search. Searches and browses rendered pages, then returns part numbers.
- `mcmaster_search`: search McMaster and return the rendered page state.
- `mcmaster_open`: open a URL, path, part number, or search phrase.
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

## Environment Variables

- `MCMASTER_NAV_PROFILE_DIR`: optional persistent Chrome profile directory. Default: a temporary isolated profile per server process.
- `MCMASTER_NAV_PAGE_TIMEOUT`: Selenium page load timeout in seconds. Default: `45`.
- `MCMASTER_NAV_SETTLE_SECONDS`: render settle delay after navigation. Default: `3`.
- `MCMASTER_NAV_AUTO_DRILL_DEPTH`: category levels to auto-open during search. Default: `2`.
- `MCMASTER_NAV_MAX_PRODUCTS`: maximum products extracted from one page. Default: `80`.
- `MCMASTER_NAV_MAX_LINKS`: maximum links extracted from one page. Default: `100`.
- `MCMASTER_NAV_TOOL_TIMEOUT`: MCP tool timeout in seconds. Default: `300`.

## Benchmark

The live benchmark starts from known part numbers, writes a part description without the part number, and checks whether `mcmaster_find_exact_part` returns the original single part number.

Latest local run:

```text
benchmark_runs/exact_part_100_v2
100/100 top-1 exact part recovery
mean lookup time: 94.992 seconds
max_pages: 20
reuse_browser: false
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
