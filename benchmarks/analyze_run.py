from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize McMaster retrieval benchmark artifacts.")
    parser.add_argument("run_dir", type=Path, help="Directory containing results.jsonl and optionally seeds.jsonl.")
    parser.add_argument(
        "--cost-per-million-tokens",
        type=float,
        default=0.0,
        help="Optional blended token cost estimate. Leave at 0 to report tokens only.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_dir = args.run_dir
    results = load_jsonl(run_dir / "results.jsonl")
    seeds = load_jsonl(run_dir / "seeds.jsonl") if (run_dir / "seeds.jsonl").exists() else []
    summary = build_summary(results, seeds=seeds, cost_per_million_tokens=args.cost_per_million_tokens)

    (run_dir / "analysis_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    write_category_csv(run_dir / "category_metrics.csv", summary["by_category"])
    write_case_csv(run_dir / "case_metrics.csv", results)
    print(json.dumps(summary, indent=2))


def build_summary(
    results: list[dict[str, Any]],
    *,
    seeds: list[dict[str, Any]],
    cost_per_million_tokens: float = 0.0,
) -> dict[str, Any]:
    scored = list(results)
    exact = [record for record in scored if record.get("found")]
    exact_single = [record for record in scored if record.get("found") and record.get("returned_count") == 1]
    seconds = [float(record.get("seconds") or 0) for record in scored]
    tokens = [int(record.get("llm_tokens") or 0) for record in scored]
    pages = [len(record.get("pages_visited") or []) for record in scored]
    token_total = sum(tokens)
    status_counts = Counter(str(record.get("status") or "unknown") for record in scored)

    return {
        "run_dir_seed_count": len(seeds),
        "scored_count": len(scored),
        "exact_count": len(exact),
        "exact_single_count": len(exact_single),
        "miss_count": len(scored) - len(exact),
        "exact_recall": safe_rate(len(exact), len(scored)),
        "exact_single_rate": safe_rate(len(exact_single), len(scored)),
        "single_return_count": sum(1 for record in scored if record.get("returned_count") == 1),
        "single_return_rate": safe_rate(sum(1 for record in scored if record.get("returned_count") == 1), len(scored)),
        "status_counts": dict(sorted(status_counts.items())),
        "latency_seconds": numeric_summary(seconds),
        "llm_tokens": {
            **numeric_summary(tokens),
            "total": token_total,
            "estimated_cost": round((token_total / 1_000_000) * cost_per_million_tokens, 6)
            if cost_per_million_tokens
            else None,
            "cost_per_million_tokens": cost_per_million_tokens or None,
        },
        "pages_visited": numeric_summary(pages),
        "by_category": summarize_by_category(scored),
        "slowest_cases": select_cases(scored, key="seconds", limit=15),
        "token_heaviest_cases": select_cases(scored, key="llm_tokens", limit=15),
        "misses": [
            case_row(record)
            for record in scored
            if not record.get("found")
        ][:50],
    }


def summarize_by_category(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in results:
        grouped[str(record.get("category") or "Unknown")].append(record)

    rows = []
    for category, records in sorted(grouped.items()):
        seconds = [float(record.get("seconds") or 0) for record in records]
        tokens = [int(record.get("llm_tokens") or 0) for record in records]
        exact = sum(1 for record in records if record.get("found"))
        single = sum(1 for record in records if record.get("returned_count") == 1)
        rows.append(
            {
                "category": category,
                "total": len(records),
                "exact": exact,
                "misses": len(records) - exact,
                "exact_rate": safe_rate(exact, len(records)),
                "single_return": single,
                "single_return_rate": safe_rate(single, len(records)),
                "mean_seconds": rounded_mean(seconds),
                "median_seconds": rounded_median(seconds),
                "mean_llm_tokens": rounded_mean(tokens),
                "median_llm_tokens": rounded_median(tokens),
                "statuses": dict(sorted(Counter(str(record.get("status") or "unknown") for record in records).items())),
            }
        )
    return rows


def numeric_summary(values: list[float | int]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean": None, "median": None, "p90": None, "p95": None, "max": None}
    sorted_values = sorted(values)
    return {
        "count": len(values),
        "mean": rounded_mean(values),
        "median": rounded_median(values),
        "p90": round(percentile(sorted_values, 0.90), 3),
        "p95": round(percentile(sorted_values, 0.95), 3),
        "max": round(max(values), 3),
    }


def percentile(sorted_values: list[float | int], q: float) -> float:
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return float(sorted_values[lower]) * (1 - fraction) + float(sorted_values[upper]) * fraction


def rounded_mean(values: list[float | int]) -> float | None:
    return round(float(statistics.mean(values)), 3) if values else None


def rounded_median(values: list[float | int]) -> float | None:
    return round(float(statistics.median(values)), 3) if values else None


def safe_rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def select_cases(results: list[dict[str, Any]], *, key: str, limit: int) -> list[dict[str, Any]]:
    return [
        case_row(record)
        for record in sorted(results, key=lambda record: float(record.get(key) or 0), reverse=True)[:limit]
    ]


def case_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "part_number": record.get("part_number"),
        "category": record.get("category"),
        "status": record.get("status"),
        "found": bool(record.get("found")),
        "returned_count": record.get("returned_count"),
        "selected_part_number": record.get("selected_part_number"),
        "seconds": record.get("seconds"),
        "llm_tokens": record.get("llm_tokens"),
        "pages_visited": len(record.get("pages_visited") or []),
        "description": record.get("description"),
        "error": record.get("error") or "",
    }


def write_category_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "category",
        "total",
        "exact",
        "misses",
        "exact_rate",
        "single_return",
        "single_return_rate",
        "mean_seconds",
        "median_seconds",
        "mean_llm_tokens",
        "median_llm_tokens",
        "statuses",
    ]
    write_csv(path, rows, fields)


def write_case_csv(path: Path, results: list[dict[str, Any]]) -> None:
    fields = [
        "part_number",
        "category",
        "status",
        "found",
        "returned_count",
        "selected_part_number",
        "seconds",
        "llm_tokens",
        "pages_visited",
        "description",
        "error",
    ]
    write_csv(path, [case_row(record) for record in results], fields)


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: json.dumps(row[field]) if isinstance(row.get(field), dict) else row.get(field, "") for field in fields})


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    for line in path.read_text().splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


if __name__ == "__main__":
    main()
