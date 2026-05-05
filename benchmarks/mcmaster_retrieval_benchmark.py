from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mcmaster_navigator_mcp.extract import PART_RE, clean_text
from mcmaster_navigator_mcp.models import PageSnapshot
from mcmaster_navigator_mcp.navigator import McMasterNavigator, _rank_links_for_query
from mcmaster_navigator_mcp.rank import derive_search_queries, normalize, term_matches

LITERAL_IDENTIFIER_RE = re.compile(
    r"\b(?=[A-Z0-9./-]{5,}\b)(?=[A-Z0-9./-]*\d)"
    r"(?:[A-Z]+\d[A-Z0-9./-]*|\d+[A-Z][A-Z0-9./-]*|\d+(?:[-/][A-Z0-9]+)+|[A-Z0-9]+(?:[-/][A-Z0-9]+)+)\b",
    re.IGNORECASE,
)


SEED_QUERIES: list[tuple[str, str]] = [
    ("Abrading & Polishing", "sanding discs"),
    ("Building & Grounds", "door hinges"),
    ("Electrical & Lighting", "toggle switch"),
    ("Fabricating", "welding clamp"),
    ("Fastening & Joining", "stainless steel socket head cap screw"),
    ("Filtering", "air filter cartridge"),
    ("Flow & Level Control", "brass ball valve"),
    ("Furniture & Storage", "drawer slides"),
    ("Hand Tools", "hex key wrench"),
    ("Hardware", "compression spring"),
    ("Heating & Cooling", "cartridge heater"),
    ("Lubricating", "grease fitting"),
    ("Material Handling", "caster wheel"),
    ("Measuring & Inspecting", "digital caliper"),
    ("Office Supplies & Signs", "safety sign"),
    ("Pipe Tubing Hose Fittings", "clear pvc tubing"),
    ("Plumbing & Janitorial", "floor drain"),
    ("Power Transmission", "timing belt pulley"),
    ("Pressure & Temperature Control", "pressure gauge"),
    ("Pulling & Lifting", "lifting eye bolt"),
    ("Raw Materials", "6061 aluminum bar"),
    ("Safety Supplies", "safety glasses"),
    ("Sawing & Cutting", "carbide drill bit"),
    ("Sealing", "buna n o ring"),
    ("Shipping", "corrugated box"),
    ("Suspending", "wire rope clip"),
    ("Bearings", "ball bearing"),
    ("Linear Motion", "linear guide rail"),
    ("Pneumatics", "pneumatic cylinder"),
    ("Adhesives", "double sided tape"),
    ("Chemicals", "epoxy adhesive"),
    ("Lab Supplies", "laboratory beaker"),
    ("Motors", "dc gearmotor"),
    ("Conveying", "conveyor roller"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark whether generated text descriptions retrieve known McMaster part numbers.",
    )
    parser.add_argument("--target", type=int, default=100, help="Number of seed parts to collect and score.")
    parser.add_argument("--per-query", type=int, default=4, help="Maximum seed parts sampled from each seed query.")
    parser.add_argument("--max-results", type=int, default=80, help="mcmaster_find_parts max_results equivalent.")
    parser.add_argument("--max-pages", type=int, default=20, help="mcmaster_find_exact_part max_pages equivalent.")
    parser.add_argument("--auto-drill-depth", type=int, default=2)
    parser.add_argument("--seed-depth", type=int, default=2)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--reuse-browser", action="store_true", help="Reuse one browser for all scored seeds.")
    parser.add_argument("--settle-seconds", type=float)
    parser.add_argument(
        "--selector",
        choices=["deterministic", "llm-schema"],
        default="deterministic",
        help="Use the existing deterministic ranker or the schema-driven LLM constraint filter.",
    )
    parser.add_argument("--llm-model", default="")
    parser.add_argument("--llm-env-file", type=Path, action="append", default=[])
    parser.add_argument("--llm-token-budget", type=int, default=2_500_000)
    parser.add_argument("--llm-max-searches", type=int, default=3)
    parser.add_argument("--llm-max-rows", type=int, default=1200)
    parser.add_argument("--llm-max-field-values", type=int, default=80)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.settle_seconds is not None:
        os.environ["MCMASTER_NAV_SETTLE_SECONDS"] = str(args.settle_seconds)
    for env_file in args.llm_env_file:
        load_env_file(env_file)
    llm_client = None
    token_budget = None
    if args.selector == "llm-schema":
        model = args.llm_model or os.getenv("OPENAI_MODEL") or os.getenv("FUSION_LLM_MODEL") or "gpt-5.4-mini"
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required for --selector llm-schema")
        token_budget = TokenBudget(args.llm_token_budget)
        llm_client = OpenAIJsonClient(api_key=api_key, model=model, budget=token_budget)

    run_dir = args.run_dir or ROOT / "benchmark_runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    seeds_path = run_dir / "seeds.jsonl"
    results_path = run_dir / "results.jsonl"
    summary_path = run_dir / "summary.json"
    csv_path = run_dir / "results.csv"

    navigator = McMasterNavigator()
    started = time.time()
    try:
        seeds = load_jsonl(seeds_path) if args.resume and seeds_path.exists() else []
        if len(seeds) < args.target:
            seeds = collect_seeds(navigator, args, seeds, seeds_path)
        else:
            print(f"seed resume: using {len(seeds)} existing seeds")

        existing_results = load_jsonl(results_path) if args.resume and results_path.exists() else []
        completed = {record["part_number"] for record in existing_results}
        results = list(existing_results)

        for index, seed in enumerate(seeds[: args.target], start=1):
            part_number = seed["part_number"]
            if part_number in completed:
                continue
            if not args.reuse_browser:
                navigator.close()
                navigator = McMasterNavigator()
            print(f"score {index}/{args.target} {part_number} [{seed['category']}]")
            result = score_seed(navigator, seed, args, llm_client=llm_client, token_budget=token_budget)
            append_jsonl(results_path, result)
            results.append(result)
            completed.add(part_number)
            rank = result.get("rank")
            rank_text = f"rank={rank}" if rank else "miss"
            print(f"  -> {rank_text}; returned={result['returned_count']}; {elapsed(started)} elapsed")

        summary = summarize(seeds[: args.target], results, args, run_dir, time.time() - started)
        if token_budget is not None:
            summary["llm_usage"] = token_budget.to_dict()
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
        write_csv(csv_path, results)
        print(json.dumps(summary, indent=2))
    finally:
        navigator.close()


def collect_seeds(
    navigator: McMasterNavigator,
    args: argparse.Namespace,
    existing: list[dict[str, Any]],
    seeds_path: Path,
) -> list[dict[str, Any]]:
    seeds = list(existing)
    seen_parts = {seed["part_number"] for seed in seeds}
    query_index = 0
    while len(seeds) < args.target:
        category, query = SEED_QUERIES[query_index % len(SEED_QUERIES)]
        query_index += 1
        pass_index = (query_index - 1) // len(SEED_QUERIES)
        sample_limit = args.per_query * (pass_index + 1)
        print(f"seed query {query_index}: {category} / {query}")
        try:
            snapshot = navigator.search(query, max_depth=args.seed_depth)
            html = navigator._sb.get_page_source()
        except Exception as exc:
            print(f"  seed query failed: {type(exc).__name__}: {exc}")
            continue

        selected = sample_products(snapshot.products, sample_limit)
        added = 0
        for product in selected:
            if product.part_number in seen_parts:
                continue
            if not is_specific_seed(product):
                continue
            description = exact_description_from_product_page(
                navigator,
                product.part_number,
                query,
                product.context,
            )
            description_source = "product_page"
            if not description:
                description_source = "listing"
                description = build_description(
                    html=html,
                    title=snapshot.title,
                    seed_query=query,
                    category=category,
                    product_name=product.name,
                    part_number=product.part_number,
                )
            if not description:
                continue
            if not is_specific_description(description):
                continue
            record = {
                "part_number": product.part_number,
                "category": category,
                "seed_query": query,
                "seed_page_url": snapshot.url,
                "seed_page_title": snapshot.title,
                "product_name": product.name,
                "description": description,
                "description_word_count": len(description.split()),
                "sources": product.sources,
                "description_source": description_source,
            }
            append_jsonl(seeds_path, record)
            seeds.append(record)
            seen_parts.add(product.part_number)
            added += 1
            print(f"  seed {len(seeds)}: {product.part_number} :: {description[:140]}")
            if len(seeds) >= args.target:
                break
        print(f"  products={len(snapshot.products)} sampled={len(selected)} added={added}")
    return seeds


def score_seed(
    navigator: McMasterNavigator,
    seed: dict[str, Any],
    args: argparse.Namespace,
    *,
    llm_client: "OpenAIJsonClient | None" = None,
    token_budget: "TokenBudget | None" = None,
) -> dict[str, Any]:
    if args.selector == "llm-schema":
        if llm_client is None or token_budget is None:
            raise RuntimeError("llm-schema selector requires an LLM client and token budget")
        return score_seed_llm_schema(navigator, seed, args, llm_client, token_budget)
    started = time.time()
    error = ""
    returned_parts: list[str] = []
    returned_products: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    try:
        result = navigator.find_exact_part(
            seed["description"],
            max_candidates=args.max_results,
            max_pages=args.max_pages,
            auto_drill_depth=args.auto_drill_depth,
        )
        selected_part_number = result["part_number"]
        returned_products = result["candidates"]
        returned_parts = [product["part_number"] for product in returned_products]
        pages = result["pages_visited"]
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    target = seed["part_number"]
    rank = returned_parts.index(target) + 1 if target in returned_parts else None
    selected_is_target = selected_part_number == target
    return {
        **seed,
        "found": selected_is_target,
        "rank": rank,
        "top1": selected_is_target,
        "top5": bool(rank and rank <= 5),
        "top10": bool(rank and rank <= 10),
        "top20": bool(rank and rank <= 20),
        "selected_part_number": selected_part_number,
        "returned_count": len(returned_parts),
        "returned_part_numbers": returned_parts[: args.max_results],
        "returned_products": returned_products[: args.max_results],
        "pages_visited": pages,
        "error": error,
        "seconds": round(time.time() - started, 3),
    }


def score_seed_llm_schema(
    navigator: McMasterNavigator,
    seed: dict[str, Any],
    args: argparse.Namespace,
    llm_client: "OpenAIJsonClient",
    token_budget: "TokenBudget",
) -> dict[str, Any]:
    started = time.time()
    target = seed["part_number"]
    error = ""
    selected_part_number = None
    matches: list[dict[str, Any]] = []
    returned_parts: list[str] = []
    pages: list[dict[str, Any]] = []
    llm_payloads: dict[str, Any] = {}
    filter_trace: list[dict[str, Any]] = []
    status = "error"
    usage_before = token_budget.used_tokens
    try:
        normalized = llm_extract_search_and_constraints(llm_client, seed["description"])
        llm_payloads["normalized"] = normalized
        search_queries = schema_search_queries(seed["description"], normalized, limit=args.llm_max_searches)
        if not search_queries:
            search_queries = derive_search_queries(seed["description"], limit=args.llm_max_searches)

        rows: list[dict[str, Any]] = []
        seen_page_urls: set[str] = set()
        search_pages: list[tuple[str, PageSnapshot]] = []
        for query in search_queries:
            if len(pages) >= args.max_pages:
                break
            page = navigator.search(query, max_depth=args.auto_drill_depth)
            if page.url not in seen_page_urls:
                pages.append(page.to_summary_dict())
                seen_page_urls.add(page.url)
                search_pages.append((query, page))
            rows = merge_rows(rows, rows_from_page(page))
            if len(rows) >= args.llm_max_rows:
                rows = rows[: args.llm_max_rows]
                break
        for query, page in search_pages:
            if len(rows) >= args.llm_max_rows:
                break
            for link in rank_schema_links(page, description=seed["description"], query=query):
                if len(pages) >= args.max_pages or len(rows) >= args.llm_max_rows:
                    break
                if link.url in seen_page_urls:
                    continue
                try:
                    linked_page = navigator.open(link.url)
                except Exception:
                    continue
                pages.append(linked_page.to_summary_dict())
                seen_page_urls.add(linked_page.url)
                rows = merge_rows(rows, rows_from_page(linked_page))

        field_summary = summarize_dynamic_fields(rows, max_values=args.llm_max_field_values)
        llm_payloads["field_summary"] = field_summary
        mapped = llm_map_constraints_to_schema(
            llm_client,
            description=seed["description"],
            normalized=normalized,
            field_summary=field_summary,
        )
        value_normalization = llm_normalize_matcher_values(
            llm_client,
            description=seed["description"],
            matchers=mapped.get("matchers", []),
            rows=rows,
            max_values=args.llm_max_field_values,
        )
        mapped["matchers"] = value_normalization.get("matchers", mapped.get("matchers", []))
        mapped["matchers"] = apply_explicit_label_values(seed["description"], mapped.get("matchers", []), rows)
        mapped["matchers"] = augment_literal_identifier_matchers(seed["description"], mapped.get("matchers", []), rows)
        llm_payloads["mapped"] = mapped
        llm_payloads["value_normalization"] = value_normalization
        matches, filter_trace = apply_constraint_matchers(rows, mapped.get("matchers", []))
        matches, variant_trace = apply_option_variant_scope(matches, mapped.get("matchers", []))
        if variant_trace:
            filter_trace.append(variant_trace)
        if should_repair_matchers(mapped.get("matchers", []), filter_trace, matches):
            llm_payloads["initial_filter_trace"] = filter_trace
            repair = llm_repair_matchers_from_live_schema(
                llm_client,
                description=seed["description"],
                normalized=normalized,
                matchers=mapped.get("matchers", []),
                field_summary=field_summary,
                rows=rows,
                filter_trace=filter_trace,
                max_values=args.llm_max_field_values,
            )
            llm_payloads["repair"] = repair
            repaired_matchers = repair.get("matchers", [])
            if repaired_matchers:
                repaired_matchers = apply_explicit_label_values(seed["description"], repaired_matchers, rows)
                repaired_matchers = augment_literal_identifier_matchers(seed["description"], repaired_matchers, rows)
                mapped["matchers"] = repaired_matchers
                llm_payloads["mapped"] = mapped
                matches, filter_trace = apply_constraint_matchers(rows, repaired_matchers)
                matches, variant_trace = apply_option_variant_scope(matches, repaired_matchers)
                if variant_trace:
                    filter_trace.append(variant_trace)
        returned_parts = unique_part_numbers(matches)
        if len(returned_parts) == 1:
            status = "unique"
            selected_part_number = returned_parts[0]
        elif len(returned_parts) > 1:
            status = "ambiguous"
        else:
            status = "unresolved"
    except BudgetExceeded as exc:
        error = f"BudgetExceeded: {exc}"
        status = "budget_exceeded"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        status = "error"

    rank = returned_parts.index(target) + 1 if target in returned_parts else None
    selected_is_target = selected_part_number == target
    return {
        **seed,
        "selector": "llm-schema",
        "found": selected_is_target,
        "status": status,
        "rank": rank,
        "top1": selected_is_target,
        "top5": bool(rank and rank <= 5),
        "top10": bool(rank and rank <= 10),
        "top20": bool(rank and rank <= 20),
        "target_in_matches": target in returned_parts,
        "selected_part_number": selected_part_number,
        "returned_count": len(returned_parts),
        "returned_part_numbers": returned_parts[: args.max_results],
        "returned_products": matches[: args.max_results],
        "pages_visited": pages,
        "filter_trace": filter_trace,
        "llm_payloads": llm_payloads,
        "llm_tokens": token_budget.used_tokens - usage_before,
        "error": error,
        "seconds": round(time.time() - started, 3),
    }


def rank_schema_links(page: PageSnapshot, *, description: str, query: str) -> list[Any]:
    ranked = _rank_links_for_query(page, f"{query} {description}")
    labels = explicit_description_labels(description)
    family_values = labels.get("family", [])
    if not family_values:
        return ranked
    return [
        link
        for _priority, _index, link in sorted(
            (
                (family_link_priority(link, family_values), index, link)
                for index, link in enumerate(ranked)
            ),
            key=lambda item: (item[0], item[1]),
        )
    ]


def family_link_priority(link: Any, family_values: list[str]) -> int:
    if not family_values:
        return 1000
    text_norm = normalize(canonical_compare_text(clean_text(str(getattr(link, "text", "")))))
    path = unquote(urlparse(str(getattr(link, "url", ""))).path)
    segments = [segment for segment in path.split("/") if segment]
    segments = [segment for segment in segments if "~~" in segment] or segments
    segment_norms = [
        normalize(canonical_compare_text(segment.replace("-", " ").replace("~", " ")))
        for segment in segments
    ]
    best = 1000
    for family in family_values:
        family_norm = normalize(canonical_compare_text(family))
        if not family_norm:
            continue
        family_tokens = set(family_norm.split())
        if text_norm == family_norm:
            best = min(best, 0)
        for segment_norm in segment_norms:
            segment_tokens = set(segment_norm.split())
            if segment_norm == family_norm:
                best = min(best, 0)
            elif family_tokens and family_tokens.issubset(segment_tokens):
                best = min(best, max(len(segment_tokens - family_tokens), 1))
    return best


def exact_description_from_product_page(
    navigator: McMasterNavigator,
    part_number: str,
    seed_query: str = "",
    listing_context: str = "",
) -> str:
    try:
        page = navigator.open(part_number)
    except Exception:
        return ""
    title = sanitize_description_piece(page.title.replace(" | McMaster-Carr", ""), part_number)
    pieces = [seed_query, title, listing_context]
    cleaned = []
    seen = set()
    for piece in pieces:
        text = sanitize_description_piece(piece, part_number)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        cleaned.append(text)
        seen.add(key)
    return ". ".join(cleaned)[:500]


def rows_from_page(page: PageSnapshot) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for product in page.products:
        rows.append(
            {
                "part_number": product.part_number,
                "family": product.family or page.title.replace(" | McMaster-Carr", ""),
                "groups": list(product.groups),
                "selected_option": product.selected_option,
                "attributes": dict(product.attributes),
                "evidence": product.context,
                "source": "product_hit",
                "url": product.url,
                "page_url": page.url,
                "page_title": page.title,
                "metadata": {},
            }
        )
    for schema in page.schemas:
        for table in schema.get("tables", []):
            for row in table.get("rows", []):
                if not isinstance(row, dict):
                    continue
                part_number = clean_text(str(row.get("part_number", "")))
                if not part_number:
                    continue
                attributes = row.get("attributes", {})
                rows.append(
                    {
                        "part_number": part_number,
                        "family": clean_text(str(row.get("family") or table.get("title") or schema.get("family_title") or page.title)),
                        "groups": [clean_text(str(group)) for group in row.get("groups", []) if clean_text(str(group))],
                        "selected_option": clean_text(str(row.get("selected_option") or "")),
                        "attributes": attributes if isinstance(attributes, dict) else {},
                        "evidence": clean_text(str(row.get("evidence") or "")),
                        "source": "schema_row",
                        "url": f"https://www.mcmaster.com/{part_number}",
                        "page_url": page.url,
                        "page_title": page.title,
                        "metadata": {
                            "option_variant": bool(row.get("option_variant")),
                            "option_field": clean_text(str(row.get("option_field") or "")),
                            "base_part_numbers": [
                                clean_text(str(part)).upper()
                                for part in row.get("base_part_numbers", [])
                                if clean_text(str(part))
                            ],
                        },
                    }
                )
    return merge_rows([], rows)


def merge_rows(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, tuple[tuple[str, str], ...], tuple[str, ...]], dict[str, Any]] = {}
    for row in [*existing, *incoming]:
        part_number = clean_text(str(row.get("part_number", ""))).upper()
        if not part_number:
            continue
        attributes = {
            clean_text(str(key)): clean_text(str(value))
            for key, value in dict(row.get("attributes", {})).items()
            if clean_text(str(key)) and clean_text(str(value))
        }
        groups = [clean_text(str(group)) for group in row.get("groups", []) if clean_text(str(group))]
        key = (
            part_number,
            tuple(sorted(attributes.items())),
            tuple(groups),
        )
        current = merged.get(key)
        clean_row = {
            **row,
            "part_number": part_number,
            "attributes": attributes,
            "groups": groups,
        }
        if current is None:
            merged[key] = clean_row
            continue
        for field in ("family", "selected_option", "evidence", "url", "page_url", "page_title"):
            value = clean_text(str(clean_row.get(field, "")))
            if value and len(value) > len(clean_text(str(current.get(field, "")))):
                current[field] = value
    return list(merged.values())


def summarize_dynamic_fields(rows: list[dict[str, Any]], *, max_values: int) -> dict[str, Any]:
    fields: dict[str, dict[str, Any]] = {}

    def add(field: str, value: str) -> None:
        value = clean_text(value)
        if not value:
            return
        slot = fields.setdefault(field, {"values": [], "count": 0})
        slot["count"] += 1
        if value not in slot["values"] and len(slot["values"]) < max_values:
            slot["values"].append(value)

    for row in rows:
        add("family", str(row.get("family", "")))
        for group in row.get("groups", []):
            add("groups", str(group))
        add("selected_option", str(row.get("selected_option", "")))
        attributes = row.get("attributes", {})
        if isinstance(attributes, dict):
            for key, value in attributes.items():
                add(f"attributes.{clean_text(str(key))}", str(value))
    return {
        "row_count": len(rows),
        "fields": [
            {"field": field, "count": data["count"], "sample_values": data["values"]}
            for field, data in sorted(fields.items())
        ],
    }


def apply_constraint_matchers(rows: list[dict[str, Any]], matchers: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_rows = list(rows)
    current = list(rows)
    trace: list[dict[str, Any]] = []
    ordered_matchers = sorted(
        [matcher for matcher in matchers if isinstance(matcher, dict)],
        key=matcher_application_priority,
    )
    for raw_matcher in ordered_matchers:
        if not isinstance(raw_matcher, dict):
            continue
        field = clean_text(str(raw_matcher.get("field", "")))
        value = clean_text(str(raw_matcher.get("value", "")))
        comparator = clean_text(str(raw_matcher.get("comparator") or "contains_all_terms"))
        accepted_values_provided = "accepted_values" in raw_matcher
        accepted_values = [
            clean_text(str(item))
            for item in raw_matcher.get("accepted_values", [])
            if clean_text(str(item))
        ]
        if not field or not value:
            continue
        before = len(unique_part_numbers(current))
        if accepted_values_provided and not accepted_values:
            trace.append(
                {
                    "constraint": clean_text(str(raw_matcher.get("constraint") or value)),
                    "field": field,
                    "value": value,
                    "comparator": comparator,
                    "accepted_values": [],
                    "before_unique_parts": before,
                    "after_unique_parts": before,
                    "skipped": True,
                    "skip_reason": "matcher has no grounded live values",
                }
            )
            continue
        filtered = [
            row
            for row in current
            if row_matches(
                row,
                field,
                value,
                comparator,
                accepted_values=accepted_values,
                accepted_values_provided=accepted_values_provided,
            )
        ]
        after = len(unique_part_numbers(filtered))
        if after == 0 and zero_matcher_can_be_skipped(field, before, trace):
            trace.append(
                {
                    "constraint": clean_text(str(raw_matcher.get("constraint") or value)),
                    "field": field,
                    "value": value,
                    "comparator": comparator,
                    "accepted_values": accepted_values[:20],
                    "before_unique_parts": before,
                    "after_unique_parts": before,
                    "skipped": True,
                    "skip_reason": zero_skip_reason(field, before),
                }
            )
            continue
        trace.append(
            {
                "constraint": clean_text(str(raw_matcher.get("constraint") or value)),
                "field": field,
                "value": value,
                "comparator": comparator,
                "accepted_values": accepted_values[:20],
                "before_unique_parts": before,
                "after_unique_parts": after,
            }
        )
        current = filtered
        if not current:
            break
    current, reconciliation_trace = reconcile_conflicting_matchers(all_rows, current, ordered_matchers, trace)
    if reconciliation_trace:
        trace.append(reconciliation_trace)
    return current, trace


def reconcile_conflicting_matchers(
    rows: list[dict[str, Any]],
    current: list[dict[str, Any]],
    matchers: list[dict[str, Any]],
    trace: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if not rows or not current or not has_grounded_conflict(trace):
        return current, None
    row_scores = score_rows_by_matchers(rows, matchers)
    if not row_scores:
        return current, None
    current_parts = set(unique_part_numbers(current))
    current_best = max((score for row, score in row_scores if clean_text(str(row.get("part_number", ""))).upper() in current_parts), default=0)
    best_score = max(score for _row, score in row_scores)
    if best_score <= current_best:
        return current, None
    best_parts = unique_scored_parts(row_scores, best_score)
    current_unique_count = len(current_parts)
    if len(best_parts) != 1 and (current_unique_count == 1 or len(best_parts) > current_unique_count):
        return current, None
    best_set = set(best_parts)
    reconciled = [row for row in rows if clean_text(str(row.get("part_number", ""))).upper() in best_set]
    if not reconciled:
        return current, None
    return reconciled, {
        "constraint": "constraint vote reconciliation",
        "field": "metadata.constraint_votes",
        "value": "",
        "comparator": "strictly_higher_match_count",
        "accepted_values": [],
        "before_unique_parts": current_unique_count,
        "after_unique_parts": len(best_parts),
        "current_best_score": current_best,
        "best_score": best_score,
        "selected_part_numbers": best_parts[:20],
    }


def has_grounded_conflict(trace: list[dict[str, Any]]) -> bool:
    for step in trace:
        if not step.get("skipped"):
            continue
        if not str(step.get("skip_reason", "")).startswith("constraint conflicts"):
            continue
        if step.get("accepted_values"):
            return True
    return False


def score_rows_by_matchers(rows: list[dict[str, Any]], matchers: list[dict[str, Any]]) -> list[tuple[dict[str, Any], int]]:
    usable: list[tuple[dict[str, Any], set[str]]] = []
    all_parts = set(unique_part_numbers(rows))
    for matcher in matchers:
        prepared = prepare_matcher(matcher)
        if prepared is None:
            continue
        field, value, comparator, accepted_values, accepted_values_provided = prepared
        matched_parts = {
            clean_text(str(row.get("part_number", ""))).upper()
            for row in rows
            if row_matches(
                row,
                field,
                value,
                comparator,
                accepted_values=accepted_values,
                accepted_values_provided=accepted_values_provided,
            )
        }
        matched_parts.discard("")
        if not matched_parts or matched_parts == all_parts:
            continue
        usable.append((matcher, matched_parts))
    if not usable:
        return []
    scored: list[tuple[dict[str, Any], int]] = []
    for row in rows:
        part = clean_text(str(row.get("part_number", ""))).upper()
        if not part:
            continue
        score = sum(1 for _matcher, matched_parts in usable if part in matched_parts)
        scored.append((row, score))
    return scored


def prepare_matcher(matcher: dict[str, Any]) -> tuple[str, str, str, list[str], bool] | None:
    field = clean_text(str(matcher.get("field", "")))
    value = clean_text(str(matcher.get("value", "")))
    comparator = clean_text(str(matcher.get("comparator") or "contains_all_terms"))
    accepted_values_provided = "accepted_values" in matcher
    accepted_values = [clean_text(str(item)) for item in matcher.get("accepted_values", []) if clean_text(str(item))]
    if not field or not value:
        return None
    if accepted_values_provided and not accepted_values:
        return None
    return field, value, comparator, accepted_values, accepted_values_provided


def unique_scored_parts(row_scores: list[tuple[dict[str, Any], int]], score: int) -> list[str]:
    seen: set[str] = set()
    parts: list[str] = []
    for row, row_score in row_scores:
        part = clean_text(str(row.get("part_number", ""))).upper()
        if row_score == score and part and part not in seen:
            seen.add(part)
            parts.append(part)
    return parts


def zero_matcher_can_be_skipped(field: str, before: int, trace: list[dict[str, Any]]) -> bool:
    if before == 1:
        return True
    if context_matcher_can_be_skipped(field, trace):
        return True
    for step in trace:
        if step.get("skipped"):
            continue
        step_field = clean_text(str(step.get("field", "")))
        step_before = int(step.get("before_unique_parts") or 0)
        step_after = int(step.get("after_unique_parts") or 0)
        if step_after <= 0 or step_after >= step_before:
            continue
        if step_field == "selected_option" or step_field == "groups" or step_field.startswith("attributes."):
            return True
    return False


def zero_skip_reason(field: str, before: int) -> str:
    if before == 1:
        return "constraint conflicts with already unique grounded match"
    if field in {"family", "groups"}:
        return "broad page-context constraint conflicts with concrete field matches"
    return "constraint conflicts with narrowed grounded match"


def context_matcher_can_be_skipped(field: str, trace: list[dict[str, Any]]) -> bool:
    if field not in {"family", "groups"}:
        return False
    for step in trace:
        step_field = clean_text(str(step.get("field", "")))
        if step.get("skipped"):
            continue
        if step_field == "selected_option" or step_field.startswith("attributes."):
            if int(step.get("after_unique_parts") or 0) > 0:
                return True
    return False


def matcher_application_priority(matcher: dict[str, Any]) -> tuple[int, str]:
    field = clean_text(str(matcher.get("field", "")))
    if field == "selected_option":
        return (0, field)
    if field.startswith("attributes."):
        return (1, field)
    if field == "groups":
        return (2, field)
    if field == "family":
        return (3, field)
    return (4, field)


def apply_option_variant_scope(rows: list[dict[str, Any]], matchers: list[Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    constrained_fields: set[str] = set()
    for matcher in matchers:
        if not isinstance(matcher, dict):
            continue
        field = clean_text(str(matcher.get("field", "")))
        if field == "row_text":
            return rows, None
        if field == "selected_option":
            constrained_fields.add("selected_option")
        if field.startswith("attributes."):
            constrained_fields.add(field.split(".", 1)[1])

    current_parts = set(unique_part_numbers(rows))
    filtered: list[dict[str, Any]] = []
    removed = 0
    for row in rows:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        option_field = clean_text(str(metadata.get("option_field") or ""))
        base_parts = {
            clean_text(str(part)).upper()
            for part in metadata.get("base_part_numbers", [])
            if clean_text(str(part))
        }
        option_is_constrained = option_field in constrained_fields or "selected_option" in constrained_fields
        if metadata.get("option_variant") and option_field and not option_is_constrained and current_parts.intersection(base_parts):
            removed += 1
            continue
        filtered.append(row)

    if not removed:
        return rows, None
    return filtered, {
        "constraint": "unrequested linked option variants",
        "field": "metadata.option_variant",
        "value": "",
        "comparator": "dynamic_option_scope",
        "accepted_values": [],
        "before_unique_parts": len(current_parts),
        "after_unique_parts": len(unique_part_numbers(filtered)),
        "removed_rows": removed,
    }


def row_matches(
    row: dict[str, Any],
    field: str,
    value: str,
    comparator: str = "contains_all_terms",
    *,
    accepted_values: list[str] | None = None,
    accepted_values_provided: bool = False,
) -> bool:
    if accepted_values_provided:
        if not accepted_values:
            return False
        accepted = set()
        for item in accepted_values:
            accepted.update(compare_value_variants(field, item))
        return any(compare_value_variants(field, item).intersection(accepted) for item in row_field_values(row, field))
    evidence = row_field_text(row, field)
    if not evidence:
        return False
    evidence_norm = normalize(canonical_compare_text(evidence))
    value_norm = normalize(canonical_compare_text(value))
    if comparator == "equals_normalized":
        return evidence_norm == value_norm
    if comparator == "contains_phrase":
        return value_norm in evidence_norm
    tokens = constraint_tokens(value)
    if comparator == "contains_any_term":
        return bool(tokens) and any(term_matches(token, evidence_norm) for token in tokens)
    return bool(tokens) and all(term_matches(token, evidence_norm) for token in tokens)


def row_field_text(row: dict[str, Any], field: str) -> str:
    return " ".join(row_field_values(row, field))


def row_field_values(row: dict[str, Any], field: str) -> list[str]:
    if field == "family":
        return [str(row.get("family", ""))]
    if field == "groups":
        return [str(group) for group in row.get("groups", [])]
    if field == "selected_option":
        return [str(row.get("selected_option", ""))]
    if field == "row_text":
        return [row_text(row)]
    if field.startswith("attributes."):
        key = field.split(".", 1)[1]
        attributes = row.get("attributes", {})
        if isinstance(attributes, dict):
            if key in attributes:
                return attribute_value_variants(field, key, str(attributes.get(key, "")))
            requested_signatures = attribute_label_signatures(key)
            values: list[str] = []
            for attribute_key, attribute_value in attributes.items():
                if requested_signatures.intersection(attribute_label_signatures(str(attribute_key))):
                    for variant in attribute_value_variants(field, str(attribute_key), str(attribute_value)):
                        if variant and variant not in values:
                            values.append(variant)
            return values
    return []


def attribute_label_signatures(value: str) -> set[str]:
    text = clean_text(value).strip(" :")
    if not text:
        return set()
    signatures = {normalize_label(text)}
    if "," in text:
        signatures.add(normalize_label(text.rsplit(",", 1)[0]))
    return {signature for signature in signatures if signature}


def attribute_value_variants(requested_field: str, actual_key: str, value: str) -> list[str]:
    values: list[str] = []

    def add(item: str) -> None:
        item = clean_text(item)
        if item and item not in values:
            values.append(item)

    add(value)
    for unit in attribute_label_units(requested_field, actual_key):
        add(f"{value} {unit}")
        stripped = strip_trailing_unit(value, unit)
        if stripped:
            add(stripped)
    return values


def attribute_label_units(*labels: str) -> list[str]:
    units: list[str] = []
    for label in labels:
        text = clean_text(label)
        if text.startswith("attributes."):
            text = text.split(".", 1)[1]
        if "," not in text:
            continue
        unit = clean_text(text.rsplit(",", 1)[1]).strip(" .")
        if unit and unit not in units:
            units.append(unit)
    return units


def strip_trailing_unit(value: str, unit: str) -> str:
    value_norm = normalize(canonical_compare_text(value))
    unit_norm = normalize(canonical_compare_text(unit))
    if not value_norm or not unit_norm:
        return ""
    if value_norm == unit_norm:
        return ""
    suffix = f" {unit_norm}"
    if value_norm.endswith(suffix):
        return value_norm[: -len(suffix)].strip()
    return ""


def compare_value_variants(field: str, value: str) -> set[str]:
    variants = attribute_value_variants(field, field, value) if field.startswith("attributes.") else [value]
    return {normalize(canonical_compare_text(variant)) for variant in variants if clean_text(variant)}


def row_text(row: dict[str, Any]) -> str:
    pieces = [str(row.get("family", "")), *[str(group) for group in row.get("groups", [])], str(row.get("selected_option", ""))]
    attributes = row.get("attributes", {})
    if isinstance(attributes, dict):
        pieces.extend(f"{key}: {value}" for key, value in attributes.items())
    pieces.append(str(row.get("evidence", "")))
    return " ".join(piece for piece in pieces if piece)


def constraint_tokens(value: str) -> list[str]:
    return [
        token
        for token in normalize(canonical_compare_text(value)).split()
        if token and token not in {"and", "or", "the", "with", "for"}
    ]


def canonical_compare_text(value: str) -> str:
    text = clean_text(value)
    text = text.replace("°", " degree ")
    text = text.replace("×", " x ")
    text = re.sub(r"\bdeg\.?\b", " degree ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bin\.?\b", " inch ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bins\.?\b", " inch ", text, flags=re.IGNORECASE)
    text = re.sub(r"\binches\b", " inch ", text, flags=re.IGNORECASE)
    return clean_text(text)


def unique_part_numbers(rows: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    parts: list[str] = []
    for row in rows:
        part = clean_text(str(row.get("part_number", ""))).upper()
        if part and part not in seen:
            seen.add(part)
            parts.append(part)
    return parts


def product_preview_sentence(text_preview: str, part_number: str) -> str:
    text = sanitize_description_piece(text_preview.replace(" | McMaster-Carr", " "), part_number)
    if not text:
        return ""
    sentences = [clean_text(sentence) for sentence in re.split(r"[.;]", text)]
    kept = []
    for sentence in sentences:
        if looks_like_nav(sentence):
            continue
        if 6 <= len(sentence.split()) <= 30:
            kept.append(sentence)
        if len(kept) >= 2:
            break
    return ". ".join(kept)


def schema_search_queries(description: str, normalized: dict[str, Any], *, limit: int) -> list[str]:
    queries: list[str] = []

    def add(value: str) -> None:
        query = clean_text(value)
        if query and query.lower() not in {item.lower() for item in queries}:
            queries.append(query)

    for match in re.finditer(r"\bFamily:\s*([^.;]+)", description, flags=re.IGNORECASE):
        add(match.group(1))
    first_segment = clean_text(re.split(r"[.;]", description, maxsplit=1)[0])
    if 1 <= len(first_segment.split()) <= 5 and not re.search(r"\d", first_segment):
        add(first_segment)
    for query in normalized.get("search_queries", []):
        if isinstance(query, str):
            add(query)
    return queries[:limit]


class BudgetExceeded(RuntimeError):
    pass


class TokenBudget:
    def __init__(self, limit: int):
        self.limit = limit
        self.used_tokens = 0
        self.estimated_tokens = 0
        self.calls = 0

    def reserve(self, estimated_tokens: int) -> None:
        if self.used_tokens + estimated_tokens > self.limit:
            raise BudgetExceeded(
                f"next estimated call would exceed token budget "
                f"({self.used_tokens}+{estimated_tokens}>{self.limit})"
            )
        self.estimated_tokens += estimated_tokens

    def record(self, tokens: int) -> None:
        self.used_tokens += max(tokens, 0)
        self.calls += 1
        if self.used_tokens > self.limit:
            raise BudgetExceeded(f"token usage exceeded budget ({self.used_tokens}>{self.limit})")

    def to_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "used_tokens": self.used_tokens,
            "estimated_reserved_tokens": self.estimated_tokens,
            "calls": self.calls,
            "remaining_tokens": max(self.limit - self.used_tokens, 0),
        }


class OpenAIJsonClient:
    def __init__(self, *, api_key: str, model: str, budget: TokenBudget):
        self.api_key = api_key
        self.model = model
        self.budget = budget

    def complete_json(self, system: str, user: str, *, max_completion_tokens: int = 900) -> dict[str, Any]:
        estimated = estimate_tokens(system) + estimate_tokens(user) + max_completion_tokens
        self.budget.reserve(estimated)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": max_completion_tokens,
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if "max_completion_tokens" in body and exc.code == 400:
                payload.pop("max_completion_tokens", None)
                data = self._retry_without_completion_cap(payload)
            else:
                raise RuntimeError(f"OpenAI API error {exc.code}: {body[:800]}") from exc
        usage = data.get("usage") or {}
        total_tokens = usage.get("total_tokens")
        if not isinstance(total_tokens, int):
            total_tokens = estimated
        self.budget.record(total_tokens)
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)

    def _retry_without_completion_cap(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 3)


def llm_extract_search_and_constraints(client: OpenAIJsonClient, description: str) -> dict[str, Any]:
    system = (
        "You convert a mechanical catalog part description into search queries and explicit constraints. "
        "Do not use supplier part numbers. Do not assume a fixed ontology. Return only JSON."
    )
    user = json.dumps(
        {
            "task": "Extract broad McMaster-Carr search queries and exact constraints from the description.",
            "description": description,
            "output_schema": {
                "search_queries": ["short broad product-family query, usually 2-4 words"],
                "constraints": [
                    {
                        "constraint": "literal requested requirement",
                        "value": "value to match on a catalog row",
                        "required": True,
                    }
                ],
            },
            "rules": [
                "The first search query must be the broad product family, not a fully specified part.",
                "Prefer product-family search queries such as socket head screw, compression spring, drawer slide.",
                "Omit dimensions, ratings, materials, counts, finishes, and option values from search queries unless they are part of the product-family noun.",
                "If the description explicitly says Family: X, include X as a search query.",
                "Constraints should contain only requirements present in the description.",
                "Keep constraints literal and short: material, size, length, rating, package quantity, model number, compatibility, finish.",
            ],
        },
        ensure_ascii=True,
    )
    result = client.complete_json(system, user, max_completion_tokens=900)
    if not isinstance(result, dict):
        raise RuntimeError("LLM normalizer returned non-object JSON")
    return result


def llm_map_constraints_to_schema(
    client: OpenAIJsonClient,
    *,
    description: str,
    normalized: dict[str, Any],
    field_summary: dict[str, Any],
) -> dict[str, Any]:
    allowed_fields = [field["field"] for field in field_summary.get("fields", []) if isinstance(field, dict)]
    system = (
        "You map requested part constraints to fields dynamically extracted from a supplier catalog page. "
        "You are not selecting a part number. Python will filter rows exactly after your mapping. "
        "Do not invent fields. Return only JSON."
    )
    user = json.dumps(
        {
            "task": "Map each required constraint to one available field so deterministic code can filter rows.",
            "description": description,
            "normalized_constraints": normalized.get("constraints", []),
            "available_fields": allowed_fields,
            "field_summary": field_summary,
            "output_schema": {
                "matchers": [
                    {
                        "constraint": "requested requirement",
                        "field": "one of available_fields or row_text",
                        "value": "literal value that must be found in that field",
                        "comparator": "contains_all_terms",
                    }
                ],
                "unmapped_constraints": ["constraint text if no available field can test it"],
            },
            "rules": [
                "Use groups for values that appear as table group headings.",
                "Use attributes.<column name> for values that appear under a specific dynamic table column.",
                "Use family only for the broad product family.",
                "Use row_text only if no specific field can represent the constraint.",
                "Do not output a matcher for a constraint unless the field summary shows the field exists.",
                "Allowed comparators are contains_all_terms, contains_phrase, equals_normalized, contains_any_term.",
                "Use contains_all_terms by default. Use equals_normalized only for short exact categorical values.",
                "Every matcher is a hard filter; overly broad or invented matchers will cause wrong results.",
            ],
        },
        ensure_ascii=True,
    )
    result = client.complete_json(system, user, max_completion_tokens=1000)
    if not isinstance(result, dict):
        raise RuntimeError("LLM mapper returned non-object JSON")
    sanitized = []
    allowed = set(allowed_fields) | {"row_text"}
    for matcher in result.get("matchers", []):
        if not isinstance(matcher, dict):
            continue
        field = clean_text(str(matcher.get("field", "")))
        value = clean_text(str(matcher.get("value", "")))
        comparator = clean_text(str(matcher.get("comparator") or "contains_all_terms"))
        if comparator not in {"contains_all_terms", "contains_phrase", "equals_normalized", "contains_any_term"}:
            comparator = "contains_all_terms"
        if field in allowed and value:
            sanitized.append(
                {
                    "constraint": clean_text(str(matcher.get("constraint") or value)),
                    "field": field,
                    "value": value,
                    "comparator": comparator,
                }
            )
    result["matchers"] = sanitized
    return result


def llm_normalize_matcher_values(
    client: OpenAIJsonClient,
    *,
    description: str,
    matchers: list[Any],
    rows: list[dict[str, Any]],
    max_values: int,
) -> dict[str, Any]:
    clean_matchers = [matcher for matcher in matchers if isinstance(matcher, dict)]
    field_values = values_for_matchers(rows, clean_matchers, max_values=max_values)
    system = (
        "You normalize both sides of catalog comparisons. For each matcher, choose the exact raw "
        "field values from the live supplier page that satisfy the requested constraint. "
        "Do not invent values. Return only JSON."
    )
    user = json.dumps(
        {
            "task": "For each matcher, select accepted raw field values from the supplied field_values.",
            "description": description,
            "matchers": clean_matchers,
            "field_values": field_values,
            "output_schema": {
                "matchers": [
                    {
                        "constraint": "same as input matcher",
                        "field": "same as input matcher",
                        "value": "same as input matcher",
                        "comparator": "same as input matcher",
                        "accepted_values": ["exact strings copied from field_values[field]"],
                    }
                ]
            },
            "rules": [
                "accepted_values must be copied exactly from field_values for that field.",
                "Use the full description to resolve aliases, units, abbreviations, and product-family wording.",
                "For groups, select group values that satisfy the constraint, not the whole row.",
                "If no supplied value satisfies the constraint, use an empty accepted_values list.",
                "Keep every input matcher in the output, only adding accepted_values.",
            ],
        },
        ensure_ascii=True,
    )
    result = client.complete_json(system, user, max_completion_tokens=1600)
    if not isinstance(result, dict):
        raise RuntimeError("LLM value normalizer returned non-object JSON")
    allowed_by_field = {
        field: set(values)
        for field, values in field_values.items()
    }
    normalized_matchers = []
    by_key = {
        (
            clean_text(str(matcher.get("constraint", ""))),
            clean_text(str(matcher.get("field", ""))),
            clean_text(str(matcher.get("value", ""))),
        ): matcher
        for matcher in clean_matchers
    }
    for matcher in result.get("matchers", []):
        if not isinstance(matcher, dict):
            continue
        field = clean_text(str(matcher.get("field", "")))
        accepted = [
            clean_text(str(value))
            for value in matcher.get("accepted_values", [])
            if clean_text(str(value)) in allowed_by_field.get(field, set())
        ]
        key = (
            clean_text(str(matcher.get("constraint", ""))),
            field,
            clean_text(str(matcher.get("value", ""))),
        )
        base = by_key.get(key, matcher)
        normalized_matchers.append({**base, "accepted_values": accepted})
    if len(normalized_matchers) != len(clean_matchers):
        seen_keys = {
            (
                clean_text(str(matcher.get("constraint", ""))),
                clean_text(str(matcher.get("field", ""))),
                clean_text(str(matcher.get("value", ""))),
            )
            for matcher in normalized_matchers
        }
        for matcher in clean_matchers:
            key = (
                clean_text(str(matcher.get("constraint", ""))),
                clean_text(str(matcher.get("field", ""))),
                clean_text(str(matcher.get("value", ""))),
            )
            if key not in seen_keys:
                normalized_matchers.append({**matcher, "accepted_values": []})
    result["matchers"] = normalized_matchers
    return result


def should_repair_matchers(matchers: list[Any], trace: list[dict[str, Any]], matches: list[dict[str, Any]]) -> bool:
    if not matches:
        return True
    if len(unique_part_numbers(matches)) == 1:
        return False
    for matcher in matchers:
        if isinstance(matcher, dict) and "accepted_values" in matcher and not matcher.get("accepted_values"):
            return True
    return any(
        not step.get("skipped") and int(step.get("before_unique_parts") or 0) > 0 and int(step.get("after_unique_parts") or 0) == 0
        for step in trace
    )


def llm_repair_matchers_from_live_schema(
    client: OpenAIJsonClient,
    *,
    description: str,
    normalized: dict[str, Any],
    matchers: list[Any],
    field_summary: dict[str, Any],
    rows: list[dict[str, Any]],
    filter_trace: list[dict[str, Any]],
    max_values: int,
) -> dict[str, Any]:
    field_values = all_field_values(rows, max_values=max_values)
    system = (
        "You repair a schema-grounded catalog matcher. Use only fields and exact raw values "
        "from the live supplier page. You are not selecting a part number. Return only JSON."
    )
    user = json.dumps(
        {
            "task": "Repair the matchers so deterministic code can filter rows without hardcoded part-family logic.",
            "description": description,
            "normalized_constraints": normalized.get("constraints", []),
            "initial_matchers": [matcher for matcher in matchers if isinstance(matcher, dict)],
            "initial_filter_trace": filter_trace,
            "field_summary": field_summary,
            "field_values": field_values,
            "output_schema": {
                "matchers": [
                    {
                        "constraint": "requested requirement",
                        "field": "one key from field_values",
                        "value": "literal requested value",
                        "comparator": "equals_normalized",
                        "accepted_values": ["exact strings copied from field_values[field]"],
                    }
                ],
                "untestable_constraints": [
                    "required description constraint that is not represented by any supplied field value"
                ],
            },
            "rules": [
                "Every accepted_values item must be copied exactly from field_values for the chosen field.",
                "Do not output a matcher unless accepted_values is non-empty.",
                "Do not choose close-looking values that contradict a requested dimension, rating, material, count, or option.",
                "Prefer specific attribute fields over row_text; use groups for group headings and family for product family only.",
                "If a required constraint cannot be grounded in the supplied values, put it in untestable_constraints.",
                "The repaired matcher set may be shorter than the input; correctness is more important than forcing every constraint.",
            ],
        },
        ensure_ascii=True,
    )
    result = client.complete_json(system, user, max_completion_tokens=2200)
    if not isinstance(result, dict):
        raise RuntimeError("LLM repair returned non-object JSON")
    allowed_by_field = {field: set(values) for field, values in field_values.items()}
    allowed_fields = set(allowed_by_field)
    repaired = []
    for matcher in result.get("matchers", []):
        if not isinstance(matcher, dict):
            continue
        field = clean_text(str(matcher.get("field", "")))
        if field not in allowed_fields:
            continue
        accepted = [
            clean_text(str(value))
            for value in matcher.get("accepted_values", [])
            if clean_text(str(value)) in allowed_by_field.get(field, set())
        ]
        if not accepted:
            continue
        comparator = clean_text(str(matcher.get("comparator") or "equals_normalized"))
        if comparator not in {"contains_all_terms", "contains_phrase", "equals_normalized", "contains_any_term"}:
            comparator = "equals_normalized"
        repaired.append(
            {
                "constraint": clean_text(str(matcher.get("constraint") or matcher.get("value") or field)),
                "field": field,
                "value": clean_text(str(matcher.get("value") or matcher.get("constraint") or "")),
                "comparator": comparator,
                "accepted_values": accepted,
            }
        )
    result["matchers"] = repaired
    result["untestable_constraints"] = [
        clean_text(str(item))
        for item in result.get("untestable_constraints", [])
        if clean_text(str(item))
    ]
    return result


def values_for_matchers(rows: list[dict[str, Any]], matchers: list[dict[str, Any]], *, max_values: int) -> dict[str, list[str]]:
    fields = []
    for matcher in matchers:
        field = clean_text(str(matcher.get("field", "")))
        if field and field not in fields:
            fields.append(field)
    values: dict[str, list[str]] = {field: [] for field in fields}
    for row in rows:
        for field in fields:
            if field == "row_text":
                continue
            for value in row_field_values(row, field):
                text = clean_text(value)
                if text and text not in values[field] and len(values[field]) < max_values:
                    values[field].append(text)
    return values


def all_field_values(rows: list[dict[str, Any]], *, max_values: int) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}

    def add(field: str, value: str) -> None:
        field = clean_text(field)
        text = clean_text(value)
        if not field or not text:
            return
        slot = values.setdefault(field, [])
        if text not in slot and len(slot) < max_values:
            slot.append(text)

    for row in rows:
        add("family", str(row.get("family", "")))
        for group in row.get("groups", []):
            add("groups", str(group))
        add("selected_option", str(row.get("selected_option", "")))
        attributes = row.get("attributes", {})
        if isinstance(attributes, dict):
            for key, value in attributes.items():
                add(f"attributes.{clean_text(str(key))}", str(value))
    return values


def apply_explicit_label_values(description: str, matchers: list[Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = explicit_description_labels(description)
    if not labels:
        return [matcher for matcher in matchers if isinstance(matcher, dict)]
    field_values = all_field_values(rows, max_values=500)
    updated = []
    for matcher in matchers:
        if not isinstance(matcher, dict):
            continue
        field = clean_text(str(matcher.get("field", "")))
        accepted = [
            clean_text(str(value))
            for value in matcher.get("accepted_values", [])
            if clean_text(str(value))
        ]
        label_values: list[str] = []
        if field == "family":
            label_values = labels.get("family", [])
        elif field == "groups":
            label_values = [
                label_value
                for label_value in labels.get("group", [])
                if label_value_matches_matcher(label_value, matcher)
            ]
        elif field == "selected_option":
            label_values = labels.get("selected option", [])
        elif field.startswith("attributes."):
            exact_field, label_values = explicit_attribute_field_for_matcher(matcher, labels, field_values)
            if exact_field:
                field = exact_field
                matcher = {**matcher, "field": exact_field}
        for label_value in label_values:
            for live_value in field_values.get(field, []):
                if same_catalog_value(label_value, live_value) and live_value not in accepted:
                    accepted.append(live_value)
        updated.append({**matcher, "accepted_values": accepted} if "accepted_values" in matcher else matcher)
    return add_missing_explicit_selected_option_matchers(labels, updated, field_values)


def add_missing_explicit_selected_option_matchers(
    labels: dict[str, list[str]],
    matchers: list[dict[str, Any]],
    field_values: dict[str, list[str]],
) -> list[dict[str, Any]]:
    if any(clean_text(str(matcher.get("field", ""))) == "selected_option" for matcher in matchers):
        return matchers
    updated = list(matchers)
    for label_value in labels.get("selected option", []):
        accepted = [
            live_value
            for live_value in field_values.get("selected_option", [])
            if same_catalog_value(label_value, live_value)
        ]
        if not accepted:
            continue
        updated.append(
            {
                "constraint": "Selected option",
                "field": "selected_option",
                "value": label_value,
                "comparator": "equals_normalized",
                "accepted_values": accepted,
            }
        )
    return updated


def augment_literal_identifier_matchers(description: str, matchers: list[Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    updated = [matcher for matcher in matchers if isinstance(matcher, dict)]
    existing_keys = {
        (
            clean_text(str(matcher.get("field", ""))),
            tuple(clean_text(str(value)) for value in matcher.get("accepted_values", []) if clean_text(str(value))),
        )
        for matcher in updated
    }
    field_values = all_field_values(rows, max_values=1000)
    for identifier in literal_identifiers(description):
        candidate = best_identifier_field(identifier, field_values)
        if candidate is None:
            continue
        field, accepted_value = candidate
        key = (field, (accepted_value,))
        if key in existing_keys:
            continue
        updated.append(
            {
                "constraint": "literal identifier",
                "field": field,
                "value": identifier,
                "comparator": "equals_normalized",
                "accepted_values": [accepted_value],
            }
        )
        existing_keys.add(key)
    return updated


def literal_identifiers(description: str) -> list[str]:
    identifiers: list[str] = []
    for match in LITERAL_IDENTIFIER_RE.finditer(description):
        identifier = clean_text(match.group(0)).strip(".,;:()[]{}")
        if not identifier or PART_RE.fullmatch(identifier):
            continue
        if identifier not in identifiers:
            identifiers.append(identifier)
    return identifiers


def best_identifier_field(identifier: str, field_values: dict[str, list[str]]) -> tuple[str, str] | None:
    exact: list[tuple[int, str, str]] = []
    containing: list[tuple[int, str, str]] = []
    identifier_norm = normalize(canonical_compare_text(identifier))
    for field, values in field_values.items():
        if field in {"family", "groups", "selected_option"}:
            continue
        if not is_identifier_field(field):
            continue
        for value in values:
            value_norm = normalize(canonical_compare_text(value))
            if value_norm == identifier_norm:
                exact.append((len(value), field, value))
            elif identifier_norm and identifier_norm in value_norm:
                containing.append((len(value), field, value))
    candidates = exact or containing
    if not candidates:
        return None
    _length, field, value = sorted(candidates, key=lambda item: (item[0], item[1], item[2]))[0]
    return field, value


def is_identifier_field(field: str) -> bool:
    field_norm = normalize(canonical_compare_text(field))
    identifier_terms = {
        "model",
        "mfr",
        "manufacturer",
        "series",
        "spec",
        "specs",
        "standard",
        "no",
        "number",
        "part",
        "grade",
        "class",
    }
    tokens = set(field_norm.split())
    return bool(tokens.intersection(identifier_terms))


def label_value_matches_matcher(label_value: str, matcher: dict[str, Any]) -> bool:
    label_norm = normalize(canonical_compare_text(label_value))
    candidates = [
        clean_text(str(matcher.get("value", ""))),
        clean_text(str(matcher.get("constraint", ""))),
    ]
    for candidate in candidates:
        candidate_norm = normalize(canonical_compare_text(candidate))
        if not candidate_norm:
            continue
        if label_norm == candidate_norm or label_norm in candidate_norm or candidate_norm in label_norm:
            return True
        candidate_tokens = set(constraint_tokens(candidate))
        label_tokens = set(constraint_tokens(label_value))
        if candidate_tokens and candidate_tokens.issubset(label_tokens):
            return True
    return False


def explicit_attribute_field_for_matcher(
    matcher: dict[str, Any],
    labels: dict[str, list[str]],
    field_values: dict[str, list[str]],
) -> tuple[str | None, list[str]]:
    keys = [
        normalize_label(str(matcher.get("constraint", ""))),
        normalize_label(str(matcher.get("value", ""))),
    ]
    field = clean_text(str(matcher.get("field", "")))
    if field.startswith("attributes."):
        field_key = normalize_label(field.split(".", 1)[1])
        keys.append(field_key)
        keys.extend(
            label
            for label in labels
            if field_key.endswith(label) or label.startswith(normalize_label(str(matcher.get("constraint", ""))))
        )
    for key in keys:
        if key not in labels:
            continue
        exact_field = next(
            (
                live_field
                for live_field in field_values
                if live_field.startswith("attributes.") and normalize_label(live_field.split(".", 1)[1]) == key
            ),
            None,
        )
        if exact_field:
            return exact_field, labels[key]
    return None, []


def explicit_description_labels(description: str) -> dict[str, list[str]]:
    labels: dict[str, list[str]] = {}
    for segment in description.split(";"):
        if ":" not in segment:
            continue
        raw_label, raw_value = segment.split(":", 1)
        label = normalize_label(raw_label)
        value = clean_text(raw_value).strip(" .")
        if not label or not value:
            continue
        labels.setdefault(label, [])
        if value not in labels[label]:
            labels[label].append(value)
    return labels


def normalize_label(value: str) -> str:
    return normalize(canonical_compare_text(value).rstrip("."))


def same_catalog_value(left: str, right: str) -> bool:
    return normalize(canonical_compare_text(left)) == normalize(canonical_compare_text(right))


def load_env_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(path)
    for line in path.read_text(errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def sample_products(products: list[Any], limit: int) -> list[Any]:
    if not products or limit <= 0:
        return []
    if len(products) <= limit:
        return products
    indexes = []
    for offset in range(limit):
        index = round(offset * (len(products) - 1) / max(limit - 1, 1))
        indexes.append(index)
    selected = []
    seen = set()
    for index in indexes:
        product = products[index]
        if product.part_number in seen:
            continue
        selected.append(product)
        seen.add(product.part_number)
    return selected


def is_specific_seed(product: Any) -> bool:
    if "link" not in getattr(product, "sources", []):
        return False
    name = clean_text(getattr(product, "name", ""))
    context = clean_text(getattr(product, "context", ""))
    if name.lower() in {"spec image", "attribute image"}:
        return False
    combined = f"{name} {context}"
    return bool(re.search(r"\d", combined)) and len(combined.split()) >= 6


def is_specific_description(description: str) -> bool:
    text = clean_text(description)
    return bool(re.search(r"\d", text)) and len(text.split()) >= 10


def build_description(
    *,
    html: str,
    title: str,
    seed_query: str,
    category: str,
    product_name: str,
    part_number: str,
) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    family = best_family(soup, title, seed_query)
    row_context = best_part_context(soup, part_number)
    pieces = [family, product_name, row_context, category, seed_query]
    cleaned: list[str] = []
    seen = set()
    for piece in pieces:
        text = sanitize_description_piece(piece, part_number)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        cleaned.append(text)
        seen.add(key)
    description = ". ".join(cleaned)
    description = re.sub(r"\s+", " ", description).strip(" .,-")
    return description[:500]


def best_family(soup: BeautifulSoup, title: str, seed_query: str) -> str:
    title_text = clean_text(title).replace(" | McMaster-Carr", "")
    if title_text and title_text.lower() != "mcmaster-carr" and not looks_like_nav(title_text):
        return title_text
    for selector in ("h1", "h2", "h3"):
        for tag in soup.find_all(selector):
            text = clean_text(tag.get_text(" ", strip=True))
            if 4 <= len(text) <= 120 and not looks_like_nav(text):
                return text
    return seed_query


def best_part_context(soup: BeautifulSoup, part_number: str) -> str:
    part_upper = part_number.upper()
    candidates: list[tuple[int, int, str]] = []
    for tag in soup.find_all(True):
        attr_blob = " ".join(str(tag.get(attr, "")) for attr in ("href", "src", "srcset", "data-src", "data-srcset"))
        text = clean_text(tag.get_text(" ", strip=True))
        has_text = part_upper in text.upper()
        has_attr = part_upper in attr_blob.upper()
        if not has_text and not has_attr:
            continue
        if has_text and len(part_number) + 8 <= len(text) <= 700:
            candidates.append((abs(len(text) - 160), len(text), text))
        for parent in tag.parents:
            parent_text = clean_text(parent.get_text(" ", strip=True))
            if part_upper not in parent_text.upper():
                continue
            if len(part_number) + 8 <= len(parent_text) <= 700:
                candidates.append((abs(len(parent_text) - 180) + 20, len(parent_text), parent_text))
                break
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def sanitize_description_piece(value: str, part_number: str) -> str:
    text = clean_text(value)
    if not text:
        return ""
    text = re.sub(re.escape(part_number), " ", text, flags=re.IGNORECASE)
    text = PART_RE.sub(" ", text)
    text = re.sub(r"\$\s*\d+(?:\.\d+)?", " ", text)
    text = re.sub(r"\bAdd to Order\b|\bCompare\b|\bPkg\.\b", " ", text, flags=re.IGNORECASE)
    text = clean_text(text)
    text = text.strip(" .,-:;")
    if len(text) < 3 or looks_like_nav(text):
        return ""
    return text


def looks_like_nav(text: str) -> bool:
    lower = text.lower()
    if any(
        marker in lower
        for marker in (
            "send cancel",
            "customer service email",
            "we will reply to your message",
            "how can we improve",
            "e-mail address",
            "enter e-mail addresses",
        )
    ):
        return True
    return lower in {
        "home",
        "order",
        "order history",
        "log in",
        "email us",
        "browse catalog",
        "customer service",
        "performance",
        "spec image",
        "attribute image",
    }


def summarize(
    seeds: list[dict[str, Any]],
    results: list[dict[str, Any]],
    args: argparse.Namespace,
    run_dir: Path,
    total_seconds: float,
) -> dict[str, Any]:
    scored = results[: len(seeds)]
    found = [record for record in scored if record.get("found")]
    ranks = [record["rank"] for record in found if record.get("rank")]
    by_category: dict[str, Counter] = defaultdict(Counter)
    by_status: Counter = Counter()
    for record in scored:
        category = record["category"]
        by_category[category]["total"] += 1
        if record.get("found"):
            by_category[category]["found"] += 1
        if record.get("top10"):
            by_category[category]["top10"] += 1
        if record.get("status"):
            by_status[record["status"]] += 1
    return {
        "run_dir": str(run_dir),
        "target": args.target,
        "seed_count": len(seeds),
        "scored_count": len(scored),
        "found_count": len(found),
        "exact_recall": round(len(found) / len(scored), 4) if scored else 0,
        "top1_count": sum(1 for record in scored if record.get("top1")),
        "top5_count": sum(1 for record in scored if record.get("top5")),
        "top10_count": sum(1 for record in scored if record.get("top10")),
        "top20_count": sum(1 for record in scored if record.get("top20")),
        "median_rank": statistics.median(ranks) if ranks else None,
        "mean_seconds": round(statistics.mean([record["seconds"] for record in scored]), 3) if scored else None,
        "total_seconds": round(total_seconds, 3),
        "parameters": {
            "selector": args.selector,
            "per_query": args.per_query,
            "max_results": args.max_results,
            "max_pages": args.max_pages,
            "auto_drill_depth": args.auto_drill_depth,
            "seed_depth": args.seed_depth,
            "reuse_browser": args.reuse_browser,
            "llm_model": args.llm_model or os.getenv("OPENAI_MODEL") or os.getenv("FUSION_LLM_MODEL") or None,
            "llm_token_budget": args.llm_token_budget if args.selector == "llm-schema" else None,
            "llm_max_searches": args.llm_max_searches if args.selector == "llm-schema" else None,
            "llm_max_rows": args.llm_max_rows if args.selector == "llm-schema" else None,
        },
        "by_status": dict(by_status),
        "by_category": {category: dict(counter) for category, counter in sorted(by_category.items())},
        "misses": [
            {
                "part_number": record["part_number"],
                "selected_part_number": record.get("selected_part_number"),
                "category": record["category"],
                "description": record["description"],
                "returned_count": record["returned_count"],
                "first_returned": record["returned_part_numbers"][:5],
                "status": record.get("status"),
                "target_in_matches": record.get("target_in_matches"),
                "error": record.get("error", ""),
            }
            for record in scored
            if not record.get("found")
        ][:25],
    }


def write_csv(path: Path, results: list[dict[str, Any]]) -> None:
    fields = [
        "part_number",
        "category",
        "seed_query",
        "description",
        "selector",
        "status",
        "found",
        "target_in_matches",
        "rank",
        "top1",
        "top5",
        "top10",
        "top20",
        "returned_count",
        "selected_part_number",
        "llm_tokens",
        "seconds",
        "seed_page_url",
        "error",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in results:
            writer.writerow({field: record.get(field, "") for field in fields})


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    for line in path.read_text().splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(record, ensure_ascii=True) + "\n")


def elapsed(started: float) -> str:
    seconds = int(time.time() - started)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    return f"{minutes}m{seconds:02d}s"


if __name__ == "__main__":
    main()
