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


def test_empty_family_values_do_not_erase_unique_attribute_match():
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

    assert benchmark.unique_part_numbers(matches) == ["A2"]
    assert trace[-1]["skipped"] is True
    assert benchmark.should_repair_matchers(matchers, trace, matches) is True


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


def test_explicit_labels_augment_accepted_live_values():
    rows = [
        {
            "part_number": "A1",
            "family": "Stainless Steel Socket Head Screws",
            "groups": ["M14 × 2 mm"],
            "attributes": {},
        }
    ]
    matchers = [
        {
            "field": "family",
            "value": "socket head cap screw",
            "accepted_values": ["socket head cap screws"],
        },
        {
            "field": "groups",
            "value": "M14 x 2 mm",
            "accepted_values": [],
        },
    ]
    description = "Family: Stainless Steel Socket Head Screws; Group: M14 x 2 mm"

    updated = benchmark.apply_explicit_label_values(description, matchers, rows)

    assert "Stainless Steel Socket Head Screws" in updated[0]["accepted_values"]
    assert "M14 × 2 mm" in updated[1]["accepted_values"]


def test_explicit_attribute_label_remaps_prefixed_live_field():
    rows = [
        {
            "part_number": "A1",
            "family": "Accessory",
            "groups": [],
            "attributes": {"For Flange OD": '1.18"', "Inline Filters Flange OD": '1.18"'},
        }
    ]
    matchers = [
        {
            "constraint": "For Flange OD",
            "field": "attributes.Inline Filters Flange OD",
            "value": '1.18"',
            "accepted_values": ['1.18"'],
        }
    ]

    updated = benchmark.apply_explicit_label_values('For Flange OD: 1.18"', matchers, rows)

    assert updated[0]["field"] == "attributes.For Flange OD"
    assert updated[0]["accepted_values"] == ['1.18"']


def test_explicit_attribute_label_remaps_prefixed_suffix_field():
    rows = [
        {
            "part_number": "A1",
            "family": "Accessory",
            "groups": [],
            "attributes": {"Max. Temp., °F": "300°", "Inline Filters Max. Temp., °F": "300°"},
        }
    ]
    matchers = [
        {
            "constraint": "Max. Temp.",
            "field": "attributes.Inline Filters Max. Temp., °F",
            "value": "300°F",
            "accepted_values": ["300°"],
        }
    ]

    updated = benchmark.apply_explicit_label_values("Max. Temp., °F: 300°", matchers, rows)

    assert updated[0]["field"] == "attributes.Max. Temp., °F"


def test_concrete_attributes_apply_before_conflicting_family_label():
    rows = [
        {
            "part_number": "A1",
            "family": "Accessory Family",
            "groups": ["Quick-Clamp Connection"],
            "attributes": {"Material": "Viton", "Size": "16"},
        },
        {
            "part_number": "A2",
            "family": "Main Family",
            "groups": ["Quick-Clamp Connection"],
            "attributes": {"Material": "Steel", "Size": "16"},
        },
    ]
    matchers = [
        {"field": "family", "value": "Main Family", "accepted_values": ["Main Family"]},
        {"field": "attributes.Material", "value": "Viton", "accepted_values": ["Viton"]},
        {"field": "attributes.Size", "value": "16", "accepted_values": ["16"]},
    ]

    matches, trace = benchmark.apply_constraint_matchers(rows, matchers)

    assert benchmark.unique_part_numbers(matches) == ["A1"]
    assert trace[-1]["skipped"] is True
