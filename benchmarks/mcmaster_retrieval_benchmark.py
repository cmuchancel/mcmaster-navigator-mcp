from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mcmaster_navigator_mcp.extract import PART_RE, clean_text
from mcmaster_navigator_mcp.navigator import McMasterNavigator


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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.settle_seconds is not None:
        import os

        os.environ["MCMASTER_NAV_SETTLE_SECONDS"] = str(args.settle_seconds)

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
            result = score_seed(navigator, seed, args)
            append_jsonl(results_path, result)
            results.append(result)
            completed.add(part_number)
            rank = result.get("rank")
            rank_text = f"rank={rank}" if rank else "miss"
            print(f"  -> {rank_text}; returned={result['returned_count']}; {elapsed(started)} elapsed")

        summary = summarize(seeds[: args.target], results, args, run_dir, time.time() - started)
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


def score_seed(navigator: McMasterNavigator, seed: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
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
    for record in scored:
        category = record["category"]
        by_category[category]["total"] += 1
        if record.get("found"):
            by_category[category]["found"] += 1
        if record.get("top10"):
            by_category[category]["top10"] += 1
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
            "per_query": args.per_query,
            "max_results": args.max_results,
            "max_pages": args.max_pages,
            "auto_drill_depth": args.auto_drill_depth,
            "seed_depth": args.seed_depth,
            "reuse_browser": args.reuse_browser,
        },
        "by_category": {category: dict(counter) for category, counter in sorted(by_category.items())},
        "misses": [
            {
                "part_number": record["part_number"],
                "selected_part_number": record.get("selected_part_number"),
                "category": record["category"],
                "description": record["description"],
                "returned_count": record["returned_count"],
                "first_returned": record["returned_part_numbers"][:5],
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
        "found",
        "rank",
        "top1",
        "top5",
        "top10",
        "top20",
        "returned_count",
        "selected_part_number",
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
