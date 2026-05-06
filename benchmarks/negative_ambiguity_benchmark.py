from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mcmaster_navigator_mcp.extract import clean_text
from mcmaster_navigator_mcp.navigator import McMasterNavigator
from mcmaster_navigator_mcp.schema_resolver import resolve_exact_part_dynamic


IMPOSSIBLE_REQUIREMENTS = (
    "Required exact constraint: material must be unobtainium.",
    "Required exact constraint: color must be transparent magenta plaid.",
    "Required exact constraint: size or capacity must be 999999 inches.",
    "Only return a part if every required exact constraint is present in the live catalog row.",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check that exact-part resolution does not collapse nonexistent or underspecified prompts to false single parts.",
    )
    parser.add_argument("--source-run", type=Path, default=ROOT / "benchmark_runs" / "llm_schema_250_general2")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--target-per-kind", type=int, default=100)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--reuse-browser", action="store_true")
    parser.add_argument("--max-pages", type=int, default=8)
    parser.add_argument("--auto-drill-depth", type=int, default=2)
    parser.add_argument("--max-candidates", type=int, default=50)
    parser.add_argument("--llm-env-file", type=Path, action="append", default=[])
    parser.add_argument("--llm-model", default="")
    parser.add_argument("--llm-max-searches", type=int, default=2)
    parser.add_argument("--llm-max-rows", type=int, default=700)
    parser.add_argument("--llm-max-field-values", type=int, default=160)
    parser.add_argument("--llm-run-token-budget", type=int, default=2_500_000)
    parser.add_argument("--llm-call-token-budget", type=int, default=2_500_000)
    parser.add_argument(
        "--case-timeout-seconds",
        type=float,
        default=float(os.getenv("MCMASTER_NAV_CASE_TIMEOUT_SECONDS", "600")),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    for env_file in args.llm_env_file:
        load_env_file(env_file)
    if args.llm_model:
        os.environ["MCMASTER_NAV_LLM_MODEL"] = args.llm_model
    os.environ["MCMASTER_NAV_LLM_MAX_SEARCHES"] = str(args.llm_max_searches)
    os.environ["MCMASTER_NAV_LLM_MAX_ROWS"] = str(args.llm_max_rows)
    os.environ["MCMASTER_NAV_LLM_MAX_FIELD_VALUES"] = str(args.llm_max_field_values)
    os.environ["MCMASTER_NAV_LLM_TOKEN_BUDGET"] = str(args.llm_call_token_budget)

    run_dir = args.run_dir or ROOT / "benchmark_runs" / datetime.now(timezone.utc).strftime("negative_ambiguity_%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    cases_path = run_dir / "cases.jsonl"
    results_path = run_dir / "results.jsonl"
    summary_path = run_dir / "summary.json"
    csv_path = run_dir / "results.csv"

    if args.resume and cases_path.exists():
        cases = load_jsonl(cases_path)
        print(f"case resume: using {len(cases)} existing cases")
    else:
        cases = build_cases(args.source_run, target_per_kind=args.target_per_kind)
        write_jsonl(cases_path, cases)
        print(f"built {len(cases)} cases")

    existing_results = load_jsonl(results_path) if args.resume and results_path.exists() else []
    completed = {record["case_id"] for record in existing_results}
    results = list(existing_results)
    used_tokens = sum(int(record.get("llm_tokens") or 0) for record in results)
    started = time.time()
    navigator = McMasterNavigator()
    try:
        for index, case in enumerate(cases, start=1):
            if case["case_id"] in completed:
                continue
            if used_tokens >= args.llm_run_token_budget:
                print(f"token budget reached: {used_tokens}/{args.llm_run_token_budget}")
                break
            if not args.reuse_browser:
                navigator.close()
                navigator = McMasterNavigator()
            print(f"score {index}/{len(cases)} {case['kind']} {case['source_part_number']} [{case['category']}]")
            result = score_case(navigator, case, args)
            append_jsonl(results_path, result)
            results.append(result)
            completed.add(case["case_id"])
            used_tokens += int(result.get("llm_tokens") or 0)
            print(
                f"  -> status={result['status']} returned={result['returned_count']} "
                f"pass={result['passed']} tokens={used_tokens}/{args.llm_run_token_budget} "
                f"{elapsed(started)} elapsed"
            )

        summary = summarize(results, cases, total_seconds=time.time() - started, args=args)
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
        write_csv(csv_path, results)
        print(json.dumps(summary, indent=2))
    finally:
        navigator.close()


def build_cases(source_run: Path, *, target_per_kind: int) -> list[dict[str, Any]]:
    seeds = load_jsonl(source_run / "seeds.jsonl")
    exact_results = {record["part_number"]: record for record in load_jsonl(source_run / "results.jsonl")}
    selected = stratified(seeds, target_per_kind)
    cases: list[dict[str, Any]] = []
    for seed in selected:
        part_number = seed["part_number"]
        exact_description = clean_text(seed.get("description") or exact_results.get(part_number, {}).get("description") or "")
        if not exact_description:
            continue
        cases.append(
            {
                "case_id": f"nonexistent:{part_number}",
                "kind": "nonexistent",
                "source_part_number": part_number,
                "category": seed["category"],
                "description": make_nonexistent_description(exact_description),
                "expected_behavior": "not_unique",
            }
        )
        cases.append(
            {
                "case_id": f"ambiguous:{part_number}",
                "kind": "ambiguous",
                "source_part_number": part_number,
                "category": seed["category"],
                "description": make_ambiguous_description(seed),
                "expected_behavior": "ambiguous_multiple",
            }
        )
    return cases


def stratified(seeds: list[dict[str, Any]], target: int) -> list[dict[str, Any]]:
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for seed in seeds:
        by_category[seed["category"]].append(seed)
    selected: list[dict[str, Any]] = []
    offset = 0
    categories = sorted(by_category)
    while len(selected) < target:
        added = False
        for category in categories:
            bucket = by_category[category]
            if offset < len(bucket):
                selected.append(bucket[offset])
                added = True
                if len(selected) >= target:
                    break
        if not added:
            break
        offset += 1
    return selected


def make_nonexistent_description(exact_description: str) -> str:
    return f"{exact_description}; {' '.join(IMPOSSIBLE_REQUIREMENTS)}"


def make_ambiguous_description(seed: dict[str, Any]) -> str:
    query = clean_text(str(seed.get("seed_query") or "part"))
    category = clean_text(str(seed.get("category") or ""))
    if category:
        return f"{query}. General {category} catalog item. No size, material, rating, finish, package quantity, or option is specified."
    return f"{query}. General catalog item. No size, material, rating, finish, package quantity, or option is specified."


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
            )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    llm_usage = ((result.get("diagnostics") or {}).get("llm_usage") or {}) if isinstance(result, dict) else {}
    status = clean_text(str(result.get("status") or "error"))
    returned_count = int(result.get("returned_count") or 0)
    selected_part = clean_text(str(result.get("part_number") or ""))
    passed = evaluate_case(case, status=status, returned_count=returned_count, selected_part=selected_part)
    return {
        **case,
        "passed": passed,
        "false_unique": status == "unique" or bool(selected_part),
        "status": status,
        "selected_part_number": selected_part or None,
        "returned_count": returned_count,
        "returned_part_numbers": result.get("returned_part_numbers", []) if isinstance(result, dict) else [],
        "candidate_count": result.get("candidate_count", 0) if isinstance(result, dict) else 0,
        "llm_tokens": int(llm_usage.get("used_tokens") or 0),
        "pages_visited": result.get("pages_visited", []) if isinstance(result, dict) else [],
        "filter_trace": result.get("filter_trace", []) if isinstance(result, dict) else [],
        "llm_payloads": result.get("llm_payloads", {}) if isinstance(result, dict) else {},
        "error": error or ((result.get("diagnostics") or {}).get("error") or "" if isinstance(result, dict) else ""),
        "seconds": round(time.time() - started, 3),
    }


def evaluate_case(case: dict[str, Any], *, status: str, returned_count: int, selected_part: str) -> bool:
    if case["kind"] == "nonexistent":
        return status != "unique" and not selected_part
    if case["kind"] == "ambiguous":
        return status == "ambiguous" and returned_count > 1 and not selected_part
    return False


class CaseTimeoutError(TimeoutError):
    pass


class case_timeout:
    def __init__(self, seconds: float):
        self.seconds = seconds
        self._old_handler: Any = None

    def __enter__(self) -> None:
        if self.seconds <= 0:
            return
        self._old_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, self._handle_timeout)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if self.seconds <= 0:
            return False
        signal.setitimer(signal.ITIMER_REAL, 0)
        if self._old_handler is not None:
            signal.signal(signal.SIGALRM, self._old_handler)
        return False

    def _handle_timeout(self, signum: int, frame: Any) -> None:
        raise CaseTimeoutError(f"case exceeded {self.seconds:g}s")


def summarize(results: list[dict[str, Any]], cases: list[dict[str, Any]], *, total_seconds: float, args: argparse.Namespace) -> dict[str, Any]:
    by_kind: dict[str, Counter] = defaultdict(Counter)
    by_status: Counter = Counter()
    for record in results:
        kind = record["kind"]
        by_kind[kind]["total"] += 1
        if record.get("passed"):
            by_kind[kind]["passed"] += 1
        if record.get("false_unique"):
            by_kind[kind]["false_unique"] += 1
        by_status[record.get("status") or "unknown"] += 1
    return {
        "case_count": len(cases),
        "scored_count": len(results),
        "passed_count": sum(1 for record in results if record.get("passed")),
        "pass_rate": round(sum(1 for record in results if record.get("passed")) / len(results), 4) if results else 0,
        "false_unique_count": sum(1 for record in results if record.get("false_unique")),
        "false_unique_rate": round(sum(1 for record in results if record.get("false_unique")) / len(results), 4) if results else 0,
        "llm_tokens": sum(int(record.get("llm_tokens") or 0) for record in results),
        "mean_seconds": round(sum(float(record.get("seconds") or 0) for record in results) / len(results), 3) if results else None,
        "total_seconds": round(total_seconds, 3),
        "by_kind": {
            kind: {
                **dict(counter),
                "pass_rate": round(counter["passed"] / counter["total"], 4) if counter["total"] else 0,
                "false_unique_rate": round(counter["false_unique"] / counter["total"], 4) if counter["total"] else 0,
            }
            for kind, counter in sorted(by_kind.items())
        },
        "by_status": dict(sorted(by_status.items())),
        "failures": [
            {
                "case_id": record["case_id"],
                "kind": record["kind"],
                "category": record["category"],
                "status": record["status"],
                "selected_part_number": record.get("selected_part_number"),
                "returned_count": record["returned_count"],
                "returned_part_numbers": record.get("returned_part_numbers", [])[:10],
                "description": record["description"],
                "error": record.get("error", ""),
            }
            for record in results
            if not record.get("passed")
        ][:50],
        "parameters": {
            "source_run": str(args.source_run),
            "target_per_kind": args.target_per_kind,
            "max_pages": args.max_pages,
            "auto_drill_depth": args.auto_drill_depth,
            "max_candidates": args.max_candidates,
            "llm_model": os.getenv("MCMASTER_NAV_LLM_MODEL") or os.getenv("FUSION_LLM_MODEL"),
            "llm_max_searches": args.llm_max_searches,
            "llm_max_rows": args.llm_max_rows,
            "llm_max_field_values": args.llm_max_field_values,
        },
    }


def write_csv(path: Path, results: list[dict[str, Any]]) -> None:
    fields = [
        "case_id",
        "kind",
        "category",
        "source_part_number",
        "passed",
        "false_unique",
        "status",
        "selected_part_number",
        "returned_count",
        "llm_tokens",
        "seconds",
        "description",
        "error",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in results:
            writer.writerow({field: record.get(field, "") for field in fields})


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


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(record, ensure_ascii=True) + "\n" for record in records))


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
