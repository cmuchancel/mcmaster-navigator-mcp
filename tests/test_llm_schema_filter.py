from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from pathlib import Path

from mcmaster_navigator_mcp import schema_resolver


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "mcmaster_retrieval_benchmark.py"
SPEC = importlib.util.spec_from_file_location("mcmaster_retrieval_benchmark", BENCHMARK)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)
FILTER_MODULES = (benchmark, schema_resolver)


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
    assert benchmark.should_repair_matchers(matchers, trace, matches) is False


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


def test_attribute_unit_header_alias_matches_value_with_embedded_unit():
    rows = [
        {
            "part_number": "1095K112",
            "family": "Grease Fittings",
            "groups": ["M10 × 1.25 mm Metric Thread"],
            "attributes": {
                "Material": "Zinc-Plated Steel",
                "Overall Ht.": "15 mm",
                "Thread Lg.": "5.5 mm",
                "Shank Lg.": "5.5 mm",
                "Hex Size": "11 mm",
                "Max. Pressure, psi": "1,450",
                "Pkg. Qty.": "10",
            },
        },
        {
            "part_number": "1095K109",
            "family": "Grease Fittings",
            "groups": ["M8 × 0.75 mm Metric Thread"],
            "attributes": {
                "Material": "Zinc-Plated Steel",
                "Overall Ht., mm": "15",
                "Thread Lg., mm": "5.5",
                "Shank Lg., mm": "5.5",
                "Hex Size, mm": "9",
                "Max. Pressure, psi": "1,450",
                "Pkg. Qty.": "10",
            },
        },
    ]
    matchers = [
        {"field": "attributes.Material", "value": "Zinc-Plated Steel", "accepted_values": ["Zinc-Plated Steel"]},
        {"field": "attributes.Overall Ht., mm", "value": "15 mm", "accepted_values": ["15"]},
        {"field": "attributes.Thread Lg., mm", "value": "5.5 mm", "accepted_values": ["5.5"]},
        {"field": "attributes.Shank Lg., mm", "value": "5.5 mm", "accepted_values": ["5.5"]},
        {"field": "attributes.Hex Size, mm", "value": "11 mm", "accepted_values": ["11"]},
        {"field": "attributes.Max. Pressure, psi", "value": "1,450 psi", "accepted_values": ["1,450"]},
        {"field": "attributes.Pkg. Qty.", "value": "10", "accepted_values": ["10"]},
        {"field": "groups", "value": "M10 × 1.25 mm Metric Thread", "accepted_values": ["M10 × 1.25 mm Metric Thread"]},
    ]

    for module in FILTER_MODULES:
        matches, trace = module.apply_constraint_matchers(rows, matchers)

        assert module.unique_part_numbers(matches) == ["1095K112"]
        assert trace[-1]["after_unique_parts"] == 1


def test_repeated_group_labels_do_not_cross_pollinate_group_matchers():
    rows = [
        {"part_number": "A1", "family": "Digital Calipers", "groups": ["Mitutoyo", "With SPC Data Output"], "attributes": {}},
        {"part_number": "A2", "family": "Digital Calipers", "groups": ["Mitutoyo", "Without SPC Data Output"], "attributes": {}},
    ]
    matchers = [
        {"constraint": "Brand/Group", "field": "groups", "value": "Mitutoyo", "accepted_values": ["Mitutoyo"]},
        {"constraint": "Data output", "field": "groups", "value": "SPC Data Output", "accepted_values": ["With SPC Data Output"]},
    ]
    description = "Group: Mitutoyo; Group: With SPC Data Output"

    for module in FILTER_MODULES:
        updated = module.apply_explicit_label_values(description, matchers, rows)
        assert updated[0]["accepted_values"] == ["Mitutoyo"]
        assert updated[1]["accepted_values"] == ["With SPC Data Output"]

        matches, _trace = module.apply_constraint_matchers(rows, updated)
        assert module.unique_part_numbers(matches) == ["A1"]


def test_explicit_group_label_adds_missing_matcher():
    rows = [
        {
            "part_number": "5105A31",
            "family": "Locking Pliers Clamps",
            "groups": ["Smooth Fixed Jaws"],
            "attributes": {"Opening Max.": '10"', "Overall Lg.": '24"'},
        },
        {
            "part_number": "5105A32",
            "family": "Locking Pliers Clamps",
            "groups": ["Smooth Pivoting Jaws"],
            "attributes": {"Opening Max.": '10"', "Overall Lg.": '24"'},
        },
    ]
    matchers = [
        {"constraint": "Opening Max.", "field": "attributes.Opening Max.", "value": '10"', "accepted_values": ['10"']},
        {"constraint": "Overall Lg.", "field": "attributes.Overall Lg.", "value": '24"', "accepted_values": ['24"']},
    ]
    description = 'Family: Locking Pliers Clamps; Group: Smooth Fixed Jaws; Opening Max.: 10"; Overall Lg.: 24"'

    for module in FILTER_MODULES:
        updated = module.apply_explicit_label_values(description, matchers, rows)

        assert updated[-1]["field"] == "groups"
        assert updated[-1]["accepted_values"] == ["Smooth Fixed Jaws"]
        matches, _trace = module.apply_constraint_matchers(rows, updated)
        assert module.unique_part_numbers(matches) == ["5105A31"]


def test_literal_identifier_is_grounded_to_live_dynamic_field():
    rows = [
        {"part_number": "A1", "family": "Digital Calipers", "groups": [], "attributes": {"Mfr. Model No.": "500-171-32"}},
        {"part_number": "A2", "family": "Digital Calipers", "groups": [], "attributes": {"Mfr. Model No.": "500-196-32"}},
    ]
    description = "Mitutoyo digital caliper, Model Number 500-171-32"

    for module in FILTER_MODULES:
        matchers = module.augment_literal_identifier_matchers(description, [], rows)
        assert matchers == [
            {
                "constraint": "literal identifier",
                "field": "attributes.Mfr. Model No.",
                "value": "500-171-32",
                "comparator": "equals_normalized",
                "accepted_values": ["500-171-32"],
            }
        ]

        matches, _trace = module.apply_constraint_matchers(rows, matchers)
        assert module.unique_part_numbers(matches) == ["A1"]


def test_literal_identifier_grounding_ignores_dimension_fields():
    rows = [
        {"part_number": "A1", "family": "Hinges", "groups": [], "attributes": {"Door Leaf Ht.": '1 3/16 "'}},
    ]
    description = 'hinge, 1-3/16" door leaf width'

    for module in FILTER_MODULES:
        assert module.augment_literal_identifier_matchers(description, [], rows) == []


def test_ungrounded_row_text_matcher_does_not_erase_unique_match():
    rows = [
        {"part_number": "A1", "family": "Demo", "groups": [], "attributes": {"Size": "1"}},
        {"part_number": "A2", "family": "Demo", "groups": [], "attributes": {"Size": "2"}},
    ]
    matchers = [
        {"constraint": "size", "field": "attributes.Size", "value": "1", "accepted_values": ["1"]},
        {"constraint": "style", "field": "row_text", "value": "straight", "accepted_values": []},
    ]

    for module in FILTER_MODULES:
        matches, trace = module.apply_constraint_matchers(rows, matchers)

        assert module.unique_part_numbers(matches) == ["A1"]
        assert trace[-1]["skipped"] is True
        assert module.should_repair_matchers(matchers, trace, matches) is False


def test_late_attribute_conflict_does_not_erase_unique_match():
    rows = [
        {"part_number": "A1", "family": "Tubing", "groups": [], "attributes": {"ID": "1mm", "OD": "3mm"}},
        {"part_number": "A2", "family": "Tubing", "groups": [], "attributes": {"ID": "2mm", "OD": "3mm", "Temper Rating": "Soft"}},
    ]
    matchers = [
        {"constraint": "ID", "field": "attributes.ID", "value": "1mm", "accepted_values": ["1mm"]},
        {"constraint": "OD", "field": "attributes.OD", "value": "3mm", "accepted_values": ["3mm"]},
        {"constraint": "soft", "field": "attributes.Temper Rating", "value": "Soft", "accepted_values": ["Soft"]},
    ]

    for module in FILTER_MODULES:
        matches, trace = module.apply_constraint_matchers(rows, matchers)

        assert module.unique_part_numbers(matches) == ["A1"]
        assert trace[-1]["skipped"] is True
        assert module.should_repair_matchers(matchers, trace, matches) is False


def test_grounded_late_constraints_can_outvote_premature_unique_match():
    rows = [
        {
            "part_number": "2325A47",
            "family": "Digital Calipers",
            "groups": ["Mitutoyo"],
            "selected_option": "",
            "attributes": {"For Caliper Measurement Range": '0" to 12" , 0 mm to 300 mm'},
        },
        {
            "part_number": "2231N13",
            "family": "Digital Calipers",
            "groups": ["Starrett"],
            "selected_option": "",
            "attributes": {
                "For Mfr. Model No.": "120-12 , 120Z-12 , 798A-12/300 , 798B-12/300 , EC799A-12/300",
                "Material": "Wood",
            },
        },
    ]
    matchers = [
        {"constraint": "product type", "field": "selected_option", "value": "Case", "accepted_values": []},
        {"constraint": "family", "field": "family", "value": "Digital Calipers", "accepted_values": ["Digital Calipers"]},
        {"constraint": "group", "field": "groups", "value": "Starrett", "accepted_values": ["Starrett"]},
        {
            "constraint": "measuring range",
            "field": "attributes.For Caliper Measurement Range",
            "value": '0" to 12" and 0 mm to 300 mm',
            "accepted_values": ['0" to 12" , 0 mm to 300 mm'],
        },
        {
            "constraint": "compatible with mfr. model no.",
            "field": "attributes.For Mfr. Model No.",
            "value": "120-12, 120Z-12, 798A-12/300, 798B-12/300, EC799A-12/300",
            "accepted_values": ["120-12 , 120Z-12 , 798A-12/300 , 798B-12/300 , EC799A-12/300"],
        },
        {"constraint": "material", "field": "attributes.Material", "value": "Wood", "accepted_values": ["Wood"]},
    ]

    for module in FILTER_MODULES:
        matches, trace = module.apply_constraint_matchers(rows, matchers)

        assert module.unique_part_numbers(matches) == ["2231N13"]
        assert trace[-1]["field"] == "metadata.constraint_votes"
        assert trace[-1]["selected_part_numbers"] == ["2231N13"]


def test_family_link_priority_prefers_exact_family_category_over_modified_sibling():
    exact = SimpleNamespace(
        text="",
        url="https://www.mcmaster.com/Stainless+Steel+Socket+Head+Screws/stainless-steel-socket-head-screws~~/",
    )
    sibling = SimpleNamespace(
        text="",
        url="https://www.mcmaster.com/Stainless+Steel+Socket+Head+Screws/left-hand-thread-stainless-steel-socket-head-screws~~/",
    )
    family_values = ["Stainless Steel Socket Head Screws"]

    for module in FILTER_MODULES:
        assert module.family_link_priority(exact, family_values) < module.family_link_priority(sibling, family_values)


def test_explicit_selected_option_label_adds_missing_matcher():
    rows = [
        {"part_number": "A1", "family": "L-Keys", "groups": [], "selected_option": "Each", "attributes": {"Drive Size": '1/8 "'}},
        {"part_number": "A2", "family": "L-Keys", "groups": [], "selected_option": "Package", "attributes": {"Drive Size": '1/8 "'}},
    ]
    matchers = [
        {"constraint": "Drive Size", "field": "attributes.Drive Size", "value": "1/8 in", "accepted_values": ['1/8 "']},
    ]
    description = "Selected option: Package; Drive Size: 1/8 \""

    for module in FILTER_MODULES:
        updated = module.apply_explicit_label_values(description, matchers, rows)
        assert updated[-1] == {
            "constraint": "Selected option",
            "field": "selected_option",
            "value": "Package",
            "comparator": "equals_normalized",
            "accepted_values": ["Package"],
        }

        matches, _trace = module.apply_constraint_matchers(rows, updated)
        assert module.unique_part_numbers(matches) == ["A2"]


def test_selected_option_value_must_preserve_requested_option_terms():
    for module in FILTER_MODULES:
        assert module.accepted_catalog_value_is_compatible("selected_option", "case for calipers", "Calipers") is False
        assert module.accepted_catalog_value_is_compatible(
            "selected_option",
            "calipers with calibration certificate",
            "Calipers with Calibration Certificate",
        ) is True


def test_selected_option_is_applied_before_attribute_matchers():
    matchers = [
        {"constraint": "Size", "field": "attributes.Size", "value": "1", "accepted_values": ["1"]},
        {"constraint": "Selected option", "field": "selected_option", "value": "Package", "accepted_values": ["Package"]},
    ]

    for module in FILTER_MODULES:
        ordered = sorted(matchers, key=module.matcher_application_priority)
        assert ordered[0]["field"] == "selected_option"


def test_bad_late_attribute_does_not_erase_narrowed_match_set():
    rows = [
        {"part_number": "A1", "family": "Demo", "groups": [], "attributes": {"A": "x", "C": "1"}},
        {"part_number": "A2", "family": "Demo", "groups": [], "attributes": {"A": "x", "B": "other", "C": "2"}},
        {"part_number": "A3", "family": "Demo", "groups": [], "attributes": {"A": "y", "B": "bad", "C": "1"}},
    ]
    matchers = [
        {"constraint": "A", "field": "attributes.A", "value": "x", "accepted_values": ["x"]},
        {"constraint": "bad mapped field", "field": "attributes.B", "value": "bad", "accepted_values": ["bad"]},
        {"constraint": "C", "field": "attributes.C", "value": "1", "accepted_values": ["1"]},
    ]

    for module in FILTER_MODULES:
        matches, trace = module.apply_constraint_matchers(rows, matchers)

        assert module.unique_part_numbers(matches) == ["A1"]
        assert trace[1]["skipped"] is True
        assert trace[1]["skip_reason"] == "constraint conflicts with narrowed grounded match"
