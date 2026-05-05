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


def test_accepted_values_match_exact_live_field_values():
    rows = [
        {"part_number": "A1", "family": "Surface-Mount Hinges with Holes", "groups": ["Aluminum"], "attributes": {}},
        {"part_number": "A2", "family": "Door Hinges", "groups": ["Steel"], "attributes": {}},
    ]
    matchers = [
        {
            "constraint": "surface mount door hinge",
            "field": "family",
            "value": "doors",
            "accepted_values": ["Surface-Mount Hinges with Holes"],
        },
        {
            "constraint": "aluminum",
            "field": "groups",
            "value": "Aluminum",
            "accepted_values": ["Aluminum"],
        },
    ]

    matches, trace = benchmark.apply_constraint_matchers(rows, matchers)

    assert benchmark.unique_part_numbers(matches) == ["A1"]
    assert trace[0]["after_unique_parts"] == 1


def test_empty_accepted_values_remain_a_hard_filter_before_repair():
    rows = [
        {"part_number": "A1", "family": "Toggle Switches", "groups": ["2 Position"], "attributes": {"Terminals": "2"}},
        {"part_number": "A2", "family": "Toggle Switches", "groups": ["3 Position"], "attributes": {"Terminals": "3"}},
    ]
    matchers = [
        {
            "constraint": "family",
            "field": "family",
            "value": "Toggle Switches",
            "accepted_values": [],
        },
        {
            "constraint": "3 terminals",
            "field": "attributes.Terminals",
            "value": "3",
            "accepted_values": ["3"],
        },
    ]

    matches, trace = benchmark.apply_constraint_matchers(rows, matchers)

    assert benchmark.unique_part_numbers(matches) == []
    assert trace[0]["after_unique_parts"] == 0


def test_schema_search_queries_prefer_explicit_family_label():
    description = "toggle switch. Family: Toggle Switches; No. of Terminals: 3"
    normalized = {"search_queries": ["toggle switch 3 terminals", "maintained toggle switch"]}

    queries = benchmark.schema_search_queries(description, normalized, limit=3)

    assert queries == ["Toggle Switches", "toggle switch", "toggle switch 3 terminals"]


def test_option_variants_require_matching_dynamic_option_constraint():
    rows = [
        {"part_number": "A1", "family": "Demo", "groups": [], "attributes": {"Size": "1"}},
        {
            "part_number": "A2",
            "family": "Demo",
            "groups": [],
            "attributes": {"Size": "1", "Wire Connection": "Screw Terminal"},
            "metadata": {
                "option_variant": True,
                "option_field": "Wire Connection",
                "base_part_numbers": ["A1"],
            },
        },
    ]

    unconstrained, trace = benchmark.apply_option_variant_scope(
        rows,
        [{"field": "attributes.Size", "value": "1", "accepted_values": ["1"]}],
    )
    constrained, _ = benchmark.apply_option_variant_scope(
        rows,
        [{"field": "attributes.Wire Connection", "value": "Screw Terminal", "accepted_values": ["Screw Terminal"]}],
    )

    assert benchmark.unique_part_numbers(unconstrained) == ["A1"]
    assert trace["removed_rows"] == 1
    assert benchmark.unique_part_numbers(constrained) == ["A1", "A2"]
