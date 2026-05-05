from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "mcmaster_retrieval_benchmark.py"
SPEC = importlib.util.spec_from_file_location("mcmaster_retrieval_benchmark", BENCHMARK)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def test_dynamic_matchers_filter_to_unique_part_without_field_hardcoding():
    rows = [
        {
            "part_number": "90696A101",
            "family": "Stainless Steel Socket Head Screws",
            "groups": ["18-8 Stainless Steel", "M14 x 2 mm"],
            "selected_option": "",
            "attributes": {"Lg.": "25 mm", "Pkg. Qty.": "5"},
        },
        {
            "part_number": "90696A111",
            "family": "Stainless Steel Socket Head Screws",
            "groups": ["18-8 Stainless Steel", "M16 x 2 mm"],
            "selected_option": "",
            "attributes": {"Lg.": "20 mm", "Pkg. Qty.": "1"},
        },
    ]
    matchers = [
        {"constraint": "18-8 stainless steel", "field": "groups", "value": "18-8 stainless steel"},
        {"constraint": "M14 x 2 mm", "field": "groups", "value": "M14 x 2 mm"},
        {"constraint": "25 mm long", "field": "attributes.Lg.", "value": "25 mm"},
        {"constraint": "pack of 5", "field": "attributes.Pkg. Qty.", "value": "5"},
    ]

    matches, trace = benchmark.apply_constraint_matchers(rows, matchers)

    assert benchmark.unique_part_numbers(matches) == ["90696A101"]
    assert trace[-1]["after_unique_parts"] == 1


def test_dynamic_matchers_can_return_ambiguous_multiple_matches():
    rows = [
        {"part_number": "A1", "family": "Demo", "groups": ["316 Stainless"], "attributes": {"Length": "1 inch"}},
        {"part_number": "A2", "family": "Demo", "groups": ["316 Stainless"], "attributes": {"Length": "2 inch"}},
    ]
    matchers = [
        {"constraint": "316 stainless", "field": "groups", "value": "316 stainless"},
    ]

    matches, trace = benchmark.apply_constraint_matchers(rows, matchers)

    assert benchmark.unique_part_numbers(matches) == ["A1", "A2"]
    assert trace[-1]["after_unique_parts"] == 2
