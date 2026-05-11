from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
BENCHMARKS = ROOT / "benchmarks"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(BENCHMARKS) not in sys.path:
    sys.path.insert(0, str(BENCHMARKS))

from mcmaster_navigator_mcp.navigator import McMasterNavigator
from mcmaster_navigator_mcp.schema_resolver import (
    DEFAULT_OPENAI_MODEL,
    collect_schema_rows,
    resolve_exact_part_dynamic,
    unique_part_numbers,
)
from mcmaster_navigator_mcp.catalog_text import normalize
from mcmaster_navigator_mcp.extract import clean_text

from negative_ambiguity_benchmark import (
    append_jsonl,
    case_timeout,
    close_navigator,
    elapsed,
    load_env_file,
    load_jsonl,
    short_error,
    stratified,
    write_jsonl,
)


MISSING_VALUE_MARKERS = {"", "-", "--", "---", "—", "———", "__", "___", "n/a", "none"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build near-exact ambiguity cases where one dynamic discriminator is omitted.",
    )
    parser.add_argument("--source-run", type=Path, default=ROOT / "benchmark_runs" / "llm_schema_250_general2")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--target", type=int, default=100)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-generation-seeds", type=int, default=250)
    parser.add_argument("--max-pages", type=int, default=8)
    parser.add_argument("--auto-drill-depth", type=int, default=2)
    parser.add_argument("--max-rows", type=int, default=700)
    parser.add_argument("--max-candidates", type=int, default=50)
    parser.add_argument("--expected-max-count", type=int, default=12)
    parser.add_argument("--pass-max-returned", type=int, default=20)
    parser.add_argument("--min-common-constraints", type=int, default=4)
    parser.add_argument("--llm-env-file", type=Path, action="append", default=[])
    parser.add_argument("--llm-model", default=DEFAULT_OPENAI_MODEL)
    parser.add_argument("--llm-max-searches", type=int, default=2)
    parser.add_argument("--llm-max-rows", type=int, default=700)
    parser.add_argument("--llm-max-field-values", type=int, default=160)
    parser.add_argument("--llm-run-token-budget", type=int, default=1_400_000)
    parser.add_argument("--llm-call-token-budget", type=int, default=2_500_000)
    parser.add_argument("--case-timeout-seconds", type=float, default=600)
    parser.add_argument("--case-retries", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    for env_file in args.llm_env_file:
        load_env_file(env_file)

    run_dir = args.run_dir or ROOT / "benchmark_runs" / datetime.now(timezone.utc).strftime("near_ambiguity_%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    cases_path = run_dir / "cases.jsonl"
    results_path = run_dir / "results.jsonl"
    summary_path = run_dir / "summary.json"
    csv_path = run_dir / "results.csv"

    if args.resume and cases_path.exists():
        cases = load_jsonl(cases_path)
        print(f"case resume: using {len(cases)} existing cases")
    else:
        cases = build_near_ambiguity_cases(args, cases_path)
        print(f"built {len(cases)} near-ambiguity cases")

    existing_results = load_jsonl(results_path) if args.resume and results_path.exists() else []
    completed = {record["case_id"] for record in existing_results}
    results = list(existing_results)
    used_tokens = sum(int(record.get("llm_tokens") or 0) for record in results)
    started = time.time()
    navigator: McMasterNavigator | None = None
    try:
        for index, case in enumerate(cases, start=1):
            if case["case_id"] in completed:
                continue
            if used_tokens >= args.llm_run_token_budget:
                print(f"token budget reached: {used_tokens}/{args.llm_run_token_budget}")
                break
            print(
                f"score {index}/{len(cases)} near_ambiguous {case['source_part_number']} "
                f"expected={case['expected_count']} [{case['category']}]"
            )
            result, navigator = score_case_with_recovery(navigator, case, args)
            append_jsonl(results_path, result)
            results.append(result)
            completed.add(case["case_id"])
            used_tokens += int(result.get("llm_tokens") or 0)
            print(
                f"  -> status={result['status']} returned={result['returned_count']} "
                f"pass={result['passed']} target={result['target_returned']} "
                f"coverage={result['expected_coverage']:.2f} tokens={used_tokens}/{args.llm_run_token_budget} "
                f"{elapsed(started)} elapsed"
            )

        summary = summarize(results, cases, total_seconds=time.time() - started, args=args)
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
        write_csv(csv_path, results)
        print(json.dumps(summary, indent=2))
    finally:
        close_navigator(navigator)


def build_near_ambiguity_cases(args: argparse.Namespace, cases_path: Path) -> list[dict[str, Any]]:
    seeds = stratified(load_jsonl(args.source_run / "seeds.jsonl"), args.max_generation_seeds)
    exact_results = {record["part_number"]: record for record in load_jsonl(args.source_run / "results.jsonl")}
    cases: list[dict[str, Any]] = []
    navigator = McMasterNavigator()
    try:
        for seed in seeds:
            if len(cases) >= args.target:
                break
            exact = exact_results.get(seed["part_number"], {})
            description = clean_text(seed.get("description") or exact.get("description") or "")
            queries = generation_search_queries(seed, description)
            if not description or not queries:
                continue
            try:
                rows, pages = collect_schema_rows(
                    navigator,
                    description=description,
                    search_queries=queries,
                    max_pages=args.max_pages,
                    auto_drill_depth=args.auto_drill_depth,
                    max_rows=args.max_rows,
                )
            except Exception as exc:
                print(f"  skip {seed['part_number']}: collect failed {type(exc).__name__}: {exc}")
                close_navigator(navigator)
                navigator = McMasterNavigator()
                continue
            case = find_near_ambiguous_case(
                seed=seed,
                rows=dedupe_rows(rows),
                pages=pages,
                expected_max_count=args.expected_max_count,
                min_common_constraints=args.min_common_constraints,
            )
            if not case:
                continue
            cases.append(case)
            append_jsonl(cases_path, case)
            print(
                f"  case {len(cases)}: {case['source_part_number']} -> "
                f"expected {case['expected_count']} via omitted {case['omitted_fields'][:4]}"
            )
    finally:
        close_navigator(navigator)
    return cases


def generation_search_queries(seed: dict[str, Any], description: str) -> list[str]:
    queries: list[str] = []

    def add(value: str) -> None:
        query = clean_text(value)
        if query and query.lower() not in {item.lower() for item in queries}:
            queries.append(query)

    for segment in description.split(";"):
        segment = clean_text(segment)
        if segment.lower().startswith("family:"):
            add(segment.split(":", 1)[1])
            break
    add(str(seed.get("seed_query") or ""))
    return queries[:2]


def find_near_ambiguous_case(
    *,
    seed: dict[str, Any],
    rows: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    expected_max_count: int,
    min_common_constraints: int,
) -> dict[str, Any] | None:
    target_part = clean_text(str(seed.get("part_number") or "")).upper()
    indexed_rows, index_by_key = index_constraint_pairs(rows)
    target_index = next(
        (
            index
            for index, item in enumerate(indexed_rows)
            if clean_text(str(item["row"].get("part_number") or "")).upper() == target_part
        ),
        None,
    )
    if target_index is None:
        return None
    target_pairs = indexed_rows[target_index]["pairs"]
    if len(target_pairs) < min_common_constraints:
        return None
    candidates: list[tuple[tuple[int, int, int, int], dict[str, Any]]] = []
    for item in indexed_rows:
        neighbor = item["row"]
        neighbor_part = clean_text(str(neighbor.get("part_number") or "")).upper()
        if not neighbor_part or neighbor_part == target_part:
            continue
        common = common_pairs(target_pairs, item["pairs"])
        if len(common) < min_common_constraints or not has_taxonomy_anchor(common):
            continue
        group_rows = [indexed_rows[index]["row"] for index in matching_row_indices(common, index_by_key)]
        group_parts = unique_part_numbers(group_rows)
        if target_part not in group_parts or not (2 <= len(group_parts) <= expected_max_count):
            continue
        variable_fields = varying_fields(group_rows)
        if not variable_fields:
            continue
        if has_omitted_selected_option_scoped_common_attribute(group_rows, common, variable_fields):
            continue
        description = describe_near_case(seed, common)
        case = {
            "case_id": f"near_ambiguous:{target_part}:{stable_case_suffix(common)}",
            "kind": "near_ambiguous",
            "source_part_number": target_part,
            "category": seed["category"],
            "description": description,
            "expected_behavior": "small_ambiguous_multiple",
            "expected_part_numbers": group_parts,
            "expected_count": len(group_parts),
            "omitted_fields": variable_fields,
            "common_constraint_count": len(common),
            "search_queries": generation_search_queries(seed, clean_text(seed.get("description") or "")),
            "generation_pages": [
                {
                    "url": page.get("url"),
                    "title": page.get("title"),
                    "product_count": page.get("product_count"),
                    "schema_count": page.get("schema_count"),
                }
                for page in pages[:8]
            ],
        }
        interesting = any(is_interesting_variation(label) for label in variable_fields)
        priority = (
            abs(len(group_parts) - 2),
            0 if interesting else 1,
            -len(common),
            len(description),
        )
        candidates.append((priority, case))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def index_constraint_pairs(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, set[int]]]:
    indexed_rows: list[dict[str, Any]] = []
    index_by_key: dict[str, set[int]] = defaultdict(set)
    for index, row in enumerate(rows):
        pairs = constraint_pairs(row, rows)
        keys = {pair["key"] for pair in pairs}
        indexed_rows.append({"row": row, "pairs": pairs, "keys": keys})
        for key in keys:
            index_by_key[key].add(index)
    return indexed_rows, index_by_key


def matching_row_indices(common: list[dict[str, str]], index_by_key: dict[str, set[int]]) -> list[int]:
    key_sets = [index_by_key.get(pair["key"], set()) for pair in common]
    if not key_sets or any(not key_set for key_set in key_sets):
        return []
    matched = set.intersection(*key_sets)
    return sorted(matched)


def constraint_pairs(row: dict[str, Any], peer_rows: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    peers = peer_rows or [row]

    def add(field: str, label: str, value: str) -> None:
        value = clean_text(value)
        if not value or normalize(value) in MISSING_VALUE_MARKERS:
            return
        pairs.append({"field": field, "label": label, "value": value, "key": pair_key(field, value)})

    add("family", "Family", str(row.get("family") or ""))
    for group in row.get("groups", []):
        add("groups", "Group", str(group))
    selected_option = clean_text(str(row.get("selected_option") or ""))
    if selected_option:
        add("selected_option", "Selected option", selected_option)
    attributes = row.get("attributes", {})
    if isinstance(attributes, dict):
        for key, value in attributes.items():
            label = clean_text(str(key))
            value_text = clean_text(str(value))
            if usable_attribute(label, value_text) and attribute_applies_to_selected_option(label, selected_option, peers):
                add(f"attributes.{label}", label, value_text)
    return pairs


def usable_attribute(label: str, value: str) -> bool:
    if not label or not value or normalize(value) in MISSING_VALUE_MARKERS:
        return False
    norm_label = normalize(label)
    if norm_label in {"each", "pair", "per ft", "per foot", "per pkg", "per pack", "pkg", "package"}:
        return False
    if norm_label.endswith(" each") or norm_label.endswith(" pair") or norm_label.endswith(" per ft") or norm_label.endswith(" per pack"):
        return False
    if "price" in norm_label or clean_text(value).startswith("$"):
        return False
    return True


def attribute_applies_to_selected_option(label: str, selected_option: str, peer_rows: list[dict[str, Any]]) -> bool:
    if not selected_option:
        return True
    label_norm = normalize(label)
    selected_norm = normalize(selected_option)
    if label_norm.startswith(selected_norm):
        return True
    for row in peer_rows:
        other_option = clean_text(str(row.get("selected_option") or ""))
        other_norm = normalize(other_option)
        if other_norm and other_norm != selected_norm and label_norm.startswith(other_norm):
            return False
    return True


def pair_key(field: str, value: str) -> str:
    return f"{normalize(field)}={normalize(value)}"


def common_pairs(left: list[dict[str, str]], right: list[dict[str, str]]) -> list[dict[str, str]]:
    right_keys = {pair["key"] for pair in right}
    return [pair for pair in left if pair["key"] in right_keys]


def has_taxonomy_anchor(pairs: list[dict[str, str]]) -> bool:
    return any(pair["field"] in {"family", "groups"} for pair in pairs)


def row_matches_pairs(row: dict[str, Any], pairs: list[dict[str, str]], peer_rows: list[dict[str, Any]] | None = None) -> bool:
    keys = {pair["key"] for pair in constraint_pairs(row, peer_rows)}
    return all(pair["key"] in keys for pair in pairs)


def varying_fields(rows: list[dict[str, Any]]) -> list[str]:
    values_by_label: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        for pair in constraint_pairs(row, rows):
            values_by_label[pair["label"]].add(pair["value"])
    labels = [label for label, values in values_by_label.items() if len(values) > 1]
    labels.sort(key=lambda label: (0 if is_interesting_variation(label) else 1, normalize(label)))
    return labels


def has_omitted_selected_option_scoped_common_attribute(
    rows: list[dict[str, Any]],
    common: list[dict[str, str]],
    variable_fields: list[str],
) -> bool:
    if "Selected option" not in variable_fields:
        return False
    option_norms = {
        normalize(clean_text(str(row.get("selected_option") or "")))
        for row in rows
        if clean_text(str(row.get("selected_option") or ""))
    }
    if len(option_norms) < 2:
        return False
    for pair in common:
        if not pair["field"].startswith("attributes."):
            continue
        label_norm = normalize(pair["label"])
        if any(option_norm and (label_norm.startswith(option_norm) or option_norm in label_norm) for option_norm in option_norms):
            return True
    return False


def is_interesting_variation(label: str) -> bool:
    text = normalize(label)
    terms = ("finish", "coating", "coated", "plating", "plated", "material", "color", "appearance", "style", "type", "option")
    return any(term in text for term in terms)


def describe_near_case(seed: dict[str, Any], pairs: list[dict[str, str]]) -> str:
    sorted_pairs = sorted(pairs, key=pair_sort_key)
    clauses = [f"{pair['label']}: {pair['value']}" for pair in sorted_pairs]
    prefix = clean_text(str(seed.get("seed_query") or "part"))
    return clean_text(f"{prefix}. " + "; ".join(clauses))


def pair_sort_key(pair: dict[str, str]) -> tuple[int, str, str]:
    field = pair["field"]
    if field == "family":
        return (0, pair["label"], pair["value"])
    if field == "groups":
        return (1, pair["label"], pair["value"])
    if field == "selected_option":
        return (2, pair["label"], pair["value"])
    return (3, pair["label"], pair["value"])


def stable_case_suffix(pairs: list[dict[str, str]]) -> str:
    text = "|".join(pair["key"] for pair in sorted(pairs, key=lambda pair: pair["key"]))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        part = clean_text(str(row.get("part_number") or "")).upper()
        if not part or part in seen:
            continue
        seen.add(part)
        deduped.append(row)
    return deduped


def score_case(navigator: McMasterNavigator, case: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    started = time.time()
    error = ""
    result: dict[str, Any] = {}
    try:
        with case_timeout(args.case_timeout_seconds):
            result = resolve_exact_part_dynamic(
                navigator,
                case["description"],
                max_candidates=args.max_candidates,
                max_pages=args.max_pages,
                auto_drill_depth=args.auto_drill_depth,
                model=args.llm_model,
                token_budget_limit=args.llm_call_token_budget,
                max_searches=args.llm_max_searches,
                max_rows=args.llm_max_rows,
                max_field_values=args.llm_max_field_values,
            )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    llm_usage = ((result.get("diagnostics") or {}).get("llm_usage") or {}) if isinstance(result, dict) else {}
    status = clean_text(str(result.get("status") or "error"))
    returned_count = int(result.get("returned_count") or 0)
    selected_part = clean_text(str(result.get("part_number") or ""))
    returned_parts = [clean_text(str(part)).upper() for part in result.get("returned_part_numbers", [])] if isinstance(result, dict) else []
    expected_parts = [clean_text(str(part)).upper() for part in case.get("expected_part_numbers", [])]
    expected_set = set(expected_parts)
    returned_set = set(returned_parts)
    expected_coverage = len(expected_set.intersection(returned_set)) / len(expected_set) if expected_set else 0
    target_returned = clean_text(str(case.get("source_part_number") or "")).upper() in returned_set
    passed = (
        status == "ambiguous"
        and not selected_part
        and 2 <= returned_count <= int(args.pass_max_returned)
        and target_returned
        and expected_set.issubset(returned_set)
    )
    return {
        **case,
        "passed": passed,
        "false_unique": status == "unique" or bool(selected_part),
        "status": status,
        "selected_part_number": selected_part or None,
        "returned_count": returned_count,
        "returned_part_numbers": returned_parts,
        "candidate_count": result.get("candidate_count", 0) if isinstance(result, dict) else 0,
        "expected_coverage": round(expected_coverage, 4),
        "target_returned": target_returned,
        "llm_tokens": int(llm_usage.get("used_tokens") or 0),
        "pages_visited": result.get("pages_visited", []) if isinstance(result, dict) else [],
        "filter_trace": result.get("filter_trace", []) if isinstance(result, dict) else [],
        "llm_payloads": result.get("llm_payloads", {}) if isinstance(result, dict) else {},
        "error": error or ((result.get("diagnostics") or {}).get("error") or "" if isinstance(result, dict) else ""),
        "seconds": round(time.time() - started, 3),
    }


def score_case_with_recovery(
    navigator: McMasterNavigator | None,
    case: dict[str, Any],
    args: argparse.Namespace,
) -> tuple[dict[str, Any], McMasterNavigator | None]:
    previous_tokens = 0
    attempts: list[dict[str, Any]] = []
    for attempt_index in range(max(int(args.case_retries), 0) + 1):
        if attempt_index > 0 or navigator is None:
            close_navigator(navigator)
            navigator = McMasterNavigator()
        result = score_case(navigator, case, args)
        attempts.append(
            {
                "attempt": attempt_index + 1,
                "status": result.get("status"),
                "passed": result.get("passed"),
                "error": result.get("error", ""),
                "llm_tokens": int(result.get("llm_tokens") or 0),
                "seconds": result.get("seconds"),
            }
        )
        if not retryable_result(result) or attempt_index >= max(int(args.case_retries), 0):
            result["llm_tokens"] = int(result.get("llm_tokens") or 0) + previous_tokens
            result["attempts"] = attempts
            result["retry_count"] = len(attempts) - 1
            return result, navigator
        previous_tokens += int(result.get("llm_tokens") or 0)
        print(f"  retrying after transient error: {short_error(result.get('error', ''))}")
        close_navigator(navigator)
        navigator = None
    raise RuntimeError("unreachable retry loop state")


def retryable_result(result: dict[str, Any]) -> bool:
    status = clean_text(str(result.get("status") or ""))
    if status not in {"error", "timeout"}:
        return False
    error = clean_text(str(result.get("error") or "")).lower()
    fragments = ("sessionnotcreatedexception", "chrome not reachable", "invalid session", "webdriver", "cannot connect to chrome", "timed out", "case exceeded")
    return not error or any(fragment in error for fragment in fragments)


def summarize(results: list[dict[str, Any]], cases: list[dict[str, Any]], *, total_seconds: float, args: argparse.Namespace) -> dict[str, Any]:
    by_status = Counter(record.get("status") or "unknown" for record in results)
    returned_counts = [int(record.get("returned_count") or 0) for record in results if record.get("status") == "ambiguous"]
    return {
        "case_count": len(cases),
        "scored_count": len(results),
        "passed_count": sum(1 for record in results if record.get("passed")),
        "pass_rate": round(sum(1 for record in results if record.get("passed")) / len(results), 4) if results else 0,
        "false_unique_count": sum(1 for record in results if record.get("false_unique")),
        "target_returned_count": sum(1 for record in results if record.get("target_returned")),
        "mean_expected_coverage": round(sum(float(record.get("expected_coverage") or 0) for record in results) / len(results), 4) if results else 0,
        "llm_tokens": sum(int(record.get("llm_tokens") or 0) for record in results),
        "mean_seconds": round(sum(float(record.get("seconds") or 0) for record in results) / len(results), 3) if results else None,
        "total_seconds": round(total_seconds, 3),
        "by_status": dict(sorted(by_status.items())),
        "returned_count_summary": {
            "min": min(returned_counts) if returned_counts else None,
            "max": max(returned_counts) if returned_counts else None,
            "mean": round(sum(returned_counts) / len(returned_counts), 3) if returned_counts else None,
        },
        "failures": [
            {
                "case_id": record["case_id"],
                "category": record["category"],
                "status": record["status"],
                "returned_count": record["returned_count"],
                "expected_count": record["expected_count"],
                "expected_part_numbers": record.get("expected_part_numbers", [])[:10],
                "returned_part_numbers": record.get("returned_part_numbers", [])[:10],
                "omitted_fields": record.get("omitted_fields", [])[:10],
                "description": record["description"],
                "error": record.get("error", ""),
            }
            for record in results
            if not record.get("passed")
        ][:50],
        "parameters": {
            "source_run": str(args.source_run),
            "target": args.target,
            "expected_max_count": args.expected_max_count,
            "pass_max_returned": args.pass_max_returned,
            "min_common_constraints": args.min_common_constraints,
            "max_pages": args.max_pages,
            "auto_drill_depth": args.auto_drill_depth,
            "max_candidates": args.max_candidates,
            "llm_model": args.llm_model,
        },
    }


def write_csv(path: Path, results: list[dict[str, Any]]) -> None:
    fields = [
        "case_id",
        "category",
        "source_part_number",
        "passed",
        "false_unique",
        "status",
        "selected_part_number",
        "returned_count",
        "expected_count",
        "expected_coverage",
        "target_returned",
        "llm_tokens",
        "seconds",
        "omitted_fields",
        "expected_part_numbers",
        "returned_part_numbers",
        "description",
        "error",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in results:
            row = {field: record.get(field, "") for field in fields}
            row["omitted_fields"] = "|".join(record.get("omitted_fields", []))
            row["expected_part_numbers"] = "|".join(record.get("expected_part_numbers", []))
            row["returned_part_numbers"] = "|".join(record.get("returned_part_numbers", []))
            writer.writerow(row)


if __name__ == "__main__":
    main()
