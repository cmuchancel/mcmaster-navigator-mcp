from __future__ import annotations

import asyncio
import atexit
import os
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from io import TextIOWrapper
from typing import Any

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .extract import normalize_target, product_url, search_url
from .navigator import McMasterNavigator


server = Server("mcmaster-navigator")
_executor = ThreadPoolExecutor(max_workers=1)
_navigator: McMasterNavigator | None = None


def _shutdown() -> None:
    global _navigator
    if _navigator is not None:
        _navigator.close()
        _navigator = None
    _executor.shutdown(wait=False, cancel_futures=True)


atexit.register(_shutdown)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="mcmaster_find_parts",
            description=(
                "Headlessly search and browse McMaster-Carr for a text query, then return "
                "part numbers found on rendered result/category pages. Use this as the "
                "default tool when the user needs part numbers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text query such as 'stainless steel socket head cap screw'."},
                    "max_results": {"type": "integer", "default": 50, "description": "Maximum unique part numbers to return."},
                    "max_pages": {"type": "integer", "default": 4, "description": "Maximum rendered pages to visit."},
                    "auto_drill_depth": {"type": "integer", "default": 2, "description": "Category levels to auto-open during search."},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="mcmaster_find_exact_part",
            description=(
                "Given a detailed text description, headlessly search McMaster-Carr, "
                "dynamically extract live table schemas, map the description onto those "
                "fields, and return a single part only when the filtered rows collapse "
                "to one candidate. Use this when the user supplied enough specs to "
                "identify one exact catalog item."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Detailed part description without a part number."},
                    "search_query": {"type": "string", "description": "Optional broad McMaster search phrase/category hint."},
                    "max_candidates": {"type": "integer", "default": 10},
                    "max_pages": {"type": "integer", "default": 20},
                    "auto_drill_depth": {"type": "integer", "default": 2},
                    "strategy": {
                        "type": "string",
                        "enum": ["dynamic_schema", "deterministic"],
                        "default": "dynamic_schema",
                        "description": "dynamic_schema uses OPENAI_API_KEY to map constraints to live page schemas; deterministic uses the legacy text ranker.",
                    },
                },
                "required": ["description"],
            },
        ),
        Tool(
            name="mcmaster_search",
            description="Headlessly search McMaster-Carr and return the current rendered page with products and navigable links.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_depth": {"type": "integer", "default": 2},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="mcmaster_open",
            description="Headlessly open a McMaster URL, path, part number, or search phrase and return extracted products and links.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "URL, path, part number, or search phrase.",
                    }
                },
                "required": ["target"],
            },
        ),
        Tool(
            name="mcmaster_current_page",
            description="Return the current rendered McMaster page state without navigating.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="mcmaster_extract_schema",
            description=(
                "Headlessly open or search a McMaster page and return dynamically extracted "
                "filters, table columns, row attributes, and part-number rows. Use this "
                "when an agent needs to inspect the live option schema for a product family."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "URL, path, part number, or search phrase."},
                    "query": {"type": "string", "description": "Search query. If provided, uses McMaster search with auto-drill."},
                    "max_depth": {"type": "integer", "default": 2},
                },
            },
        ),
        Tool(
            name="mcmaster_follow_link",
            description="Follow a link from the current page by index, text, or URL, then return the new rendered page state.",
            inputSchema={
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Link index from a previous page result."},
                    "text": {"type": "string", "description": "Case-insensitive text to match in the current page links."},
                    "url": {"type": "string", "description": "Explicit McMaster URL or path."},
                },
            },
        ),
        Tool(
            name="mcmaster_back",
            description="Go back in the headless browser history and return the new page state.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="mcmaster_url",
            description="Generate a McMaster product or search URL without launching a browser.",
            inputSchema={
                "type": "object",
                "properties": {
                    "part_number": {"type": "string"},
                    "search_query": {"type": "string"},
                    "target": {"type": "string"},
                },
            },
        ),
        Tool(
            name="mcmaster_doctor",
            description="Return environment diagnostics for the headless McMaster navigator.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="mcmaster_close_browser",
            description="Close and reset the headless browser session.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        args = arguments or {}
        if name == "mcmaster_url":
            return [_text(_url_result(args))]
        action = _tool_to_action(name)
        result = await _call_worker(action, args)
        return [_text(result)]
    except Exception as exc:
        return [_text({"error": f"{type(exc).__name__}: {exc}"})]


def _tool_to_action(name: str) -> str:
    mapping = {
        "mcmaster_find_parts": "find_parts",
        "mcmaster_find_exact_part": "find_exact_part",
        "mcmaster_search": "search",
        "mcmaster_open": "open",
        "mcmaster_current_page": "current_page",
        "mcmaster_extract_schema": "extract_schema",
        "mcmaster_follow_link": "follow_link",
        "mcmaster_back": "back",
        "mcmaster_doctor": "doctor",
        "mcmaster_close_browser": "close_browser",
    }
    if name not in mapping:
        raise ValueError(f"Unknown tool: {name}")
    return mapping[name]


async def _call_worker(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    timeout = float(os.environ.get("MCMASTER_NAV_TOOL_TIMEOUT", "300"))
    return await asyncio.wait_for(
        loop.run_in_executor(_executor, _dispatch, action, payload),
        timeout=timeout,
    )


def _get_navigator() -> McMasterNavigator:
    global _navigator
    if _navigator is None:
        _navigator = McMasterNavigator()
    return _navigator


def _dispatch(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            return _dispatch_once(action, payload)
        except Exception as exc:
            last_error = exc
            if attempt == 0 and _is_recoverable_browser_error(exc):
                _reset_navigator()
                continue
            raise
    assert last_error is not None
    raise last_error


def _dispatch_once(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    global _navigator
    navigator = _get_navigator()
    if action == "doctor":
        return navigator.doctor()
    if action == "search":
        return navigator.search(
            payload["query"],
            max_depth=payload.get("max_depth"),
        ).to_dict()
    if action == "find_parts":
        return navigator.find_parts(
            payload["query"],
            max_results=int(payload.get("max_results", 50)),
            max_pages=int(payload.get("max_pages", 4)),
            auto_drill_depth=payload.get("auto_drill_depth"),
        ).to_dict()
    if action == "find_exact_part":
        strategy = (payload.get("strategy") or "dynamic_schema").strip()
        if strategy == "dynamic_schema":
            from .schema_resolver import resolve_exact_part_dynamic

            return resolve_exact_part_dynamic(
                navigator,
                payload["description"],
                search_query=payload.get("search_query"),
                max_candidates=int(payload.get("max_candidates", 10)),
                max_pages=int(payload.get("max_pages", 20)),
                auto_drill_depth=payload.get("auto_drill_depth"),
            )
        if strategy != "deterministic":
            raise ValueError(f"Unknown mcmaster_find_exact_part strategy: {strategy}")
        return navigator.find_exact_part(
            payload["description"],
            search_query=payload.get("search_query"),
            max_candidates=int(payload.get("max_candidates", 10)),
            max_pages=int(payload.get("max_pages", 20)),
            auto_drill_depth=payload.get("auto_drill_depth"),
        )
    if action == "open":
        return navigator.open(payload["target"]).to_dict()
    if action == "current_page":
        return navigator.current_page().to_dict()
    if action == "extract_schema":
        query = (payload.get("query") or "").strip()
        target = (payload.get("target") or "").strip()
        if query:
            snapshot = navigator.search(query, max_depth=payload.get("max_depth"))
        elif target:
            snapshot = navigator.open(target)
        else:
            snapshot = navigator.current_page()
        return {
            "url": snapshot.url,
            "title": snapshot.title,
            "page_type": snapshot.page_type,
            "product_count": len(snapshot.products),
            "part_numbers": snapshot.part_numbers,
            "schema_count": len(snapshot.schemas),
            "schemas": snapshot.schemas,
            "links": [link.to_dict() for link in snapshot.links],
            "diagnostics": snapshot.diagnostics,
        }
    if action == "follow_link":
        index = payload.get("index")
        return navigator.follow_link(
            index=int(index) if index is not None else None,
            text=payload.get("text"),
            url=payload.get("url"),
        ).to_dict()
    if action == "back":
        return navigator.back().to_dict()
    if action == "close_browser":
        _reset_navigator()
        return {"closed": True}
    raise ValueError(f"Unknown action: {action}")


def _reset_navigator() -> None:
    global _navigator
    if _navigator is not None:
        _navigator.close()
        _navigator = None


def _is_recoverable_browser_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in text
        for marker in (
            "sessionnotcreatedexception",
            "nosuchwindowexception",
            "chrome not reachable",
            "target window already closed",
            "web view not found",
        )
    )


def _url_result(args: dict[str, Any]) -> dict[str, str]:
    part_number = (args.get("part_number") or "").strip()
    query = (args.get("search_query") or "").strip()
    target = (args.get("target") or "").strip()
    if part_number:
        return {"type": "product", "url": product_url(part_number)}
    if query:
        return {"type": "search", "url": search_url(query)}
    if target:
        return {"type": "target", "url": normalize_target(target)}
    raise ValueError("Provide part_number, search_query, or target")


def _text(value: dict[str, Any]) -> TextContent:
    return TextContent(type="text", text=json.dumps(value, indent=2))


async def main_async() -> None:
    protocol_stdout = anyio.wrap_file(TextIOWrapper(sys.stdout.buffer, encoding="utf-8"))
    sys.stdout = sys.stderr
    async with stdio_server(stdout=protocol_stdout) as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
