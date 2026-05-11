from types import SimpleNamespace

from mcmaster_navigator_mcp import schema_resolver


FILTER_MODULES = (schema_resolver,)


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

    matches, trace = schema_resolver.apply_constraint_matchers(rows, matchers)

    assert schema_resolver.unique_part_numbers(matches) == ["90696A101"]
    assert trace[-1]["after_unique_parts"] == 1


def test_dynamic_matchers_can_return_ambiguous_multiple_matches():
    rows = [
        {"part_number": "A1", "family": "Demo", "groups": ["316 Stainless"], "attributes": {"Length": "1 inch"}},
        {"part_number": "A2", "family": "Demo", "groups": ["316 Stainless"], "attributes": {"Length": "2 inch"}},
    ]
    matchers = [
        {"constraint": "316 stainless", "field": "groups", "value": "316 stainless"},
    ]

    matches, trace = schema_resolver.apply_constraint_matchers(rows, matchers)

    assert schema_resolver.unique_part_numbers(matches) == ["A1", "A2"]
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

    matches, trace = schema_resolver.apply_constraint_matchers(rows, matchers)

    assert schema_resolver.unique_part_numbers(matches) == ["A1"]
    assert trace[0]["after_unique_parts"] == 1


def test_grounded_accepted_values_filter_even_when_value_is_omitted():
    rows = [
        {
            "part_number": "A1",
            "family": "Digital Calipers",
            "groups": ["Mitutoyo", "Mitutoyo Cords"],
            "attributes": {"Connection": "Mitutoyo Data Out Switch C x 10-Pin Mitutoyo Connector"},
        },
        {
            "part_number": "A2",
            "family": "Digital Calipers",
            "groups": ["Mitutoyo", "Other Cords"],
            "attributes": {"Connection": "USB"},
        },
    ]
    matchers = [
        {"constraint": "family", "field": "family", "accepted_values": ["Digital Calipers"]},
        {"constraint": "group", "field": "groups", "accepted_values": ["Mitutoyo Cords"]},
        {
            "constraint": "connection",
            "field": "attributes.Connection",
            "accepted_values": ["Mitutoyo Data Out Switch C x 10-Pin Mitutoyo Connector"],
        },
    ]

    for module in FILTER_MODULES:
        matches, trace = module.apply_constraint_matchers(rows, matchers)

        assert module.unique_part_numbers(matches) == ["A1"]
        assert trace[-1]["after_unique_parts"] == 1


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

    matches, trace = schema_resolver.apply_constraint_matchers(rows, matchers)

    assert schema_resolver.unique_part_numbers(matches) == ["A2"]
    assert trace[-1]["skipped"] is True
    assert schema_resolver.should_repair_matchers(matchers, trace, matches) is False


def test_schema_search_queries_prefer_explicit_family_label():
    description = "toggle switch. Family: Toggle Switches; No. of Terminals: 3"
    normalized = {"search_queries": ["toggle switch 3 terminals", "maintained toggle switch"]}

    queries = schema_resolver.schema_search_queries(description, normalized, limit=3)

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

    unconstrained, trace = schema_resolver.apply_option_variant_scope(
        rows,
        [{"field": "attributes.Size", "value": "1", "accepted_values": ["1"]}],
    )
    constrained, _ = schema_resolver.apply_option_variant_scope(
        rows,
        [{"field": "attributes.Wire Connection", "value": "Screw Terminal", "accepted_values": ["Screw Terminal"]}],
    )

    assert schema_resolver.unique_part_numbers(unconstrained) == ["A1"]
    assert trace["removed_rows"] == 1
    assert schema_resolver.unique_part_numbers(constrained) == ["A1", "A2"]


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

    updated = schema_resolver.apply_explicit_label_values(description, matchers, rows)

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

    updated = schema_resolver.apply_explicit_label_values('For Flange OD: 1.18"', matchers, rows)

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

    updated = schema_resolver.apply_explicit_label_values("Max. Temp., °F: 300°", matchers, rows)

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

    matches, trace = schema_resolver.apply_constraint_matchers(rows, matchers)

    assert schema_resolver.unique_part_numbers(matches) == ["A1"]
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


def test_accepted_catalog_value_guard_rejects_semantic_mismatch_and_page_artifacts():
    artifact = (
        'Steel Eyebolt without Shoulder - for Lifting, 3/4"-10 Thread Size, '
        '1-5/8" Thread Length Product Detail Quantity Each Delivers tomorrow 7-9 am ADD TO ORDER'
    )

    for module in FILTER_MODULES:
        assert module.significant_catalog_tokens("Zinc-Plated") == ["zinc", "plated"]
        assert "lb" not in module.significant_catalog_tokens("1,400 lb")
        assert module.accepted_catalog_value_is_compatible("attributes.Appearance", "Zinc-Plated", "Dull") is False
        assert module.accepted_catalog_value_is_compatible("groups", "Zinc-Plated", "Zinc-Plated Steel") is True
        assert module.accepted_catalog_value_is_compatible("attributes.Vert. Cap., lb.", "1,400 lb", "1,400") is True
        assert module.accepted_catalog_value_is_compatible("attributes.No. of Flanges", "without flanges", "—") is True
        assert module.accepted_catalog_value_is_compatible("attributes.No. of Flanges", "2 flanges", "—") is False
        assert module.accepted_catalog_value_is_compatible("attributes.Features", "none", "__") is True
        assert module.accepted_catalog_value_is_compatible("attributes.Features", "Filter Access Port", "__") is False
        assert module.accepted_catalog_value_is_compatible("attributes.Thread Size", '3/4"-10', artifact) is False


def test_openai_json_client_retries_truncated_json_with_larger_completion_cap():
    for module in FILTER_MODULES:
        class FakeClient(module.OpenAIJsonClient):
            def __init__(self):
                self.api_key = "test"
                self.model = "fake-model"
                self.usage = module.TokenUsage()
                self.payload_caps = []

            def _request_completion(self, payload):
                self.payload_caps.append(payload["max_completion_tokens"])
                if len(self.payload_caps) == 1:
                    return {
                        "usage": {"total_tokens": 11},
                        "choices": [
                            {
                                "message": {"content": '{"matchers": [{"field": "groups", "value": "Iron"'},
                                "finish_reason": "length",
                            }
                        ],
                    }
                return {
                    "usage": {"total_tokens": 12},
                    "choices": [
                        {
                            "message": {"content": '{"matchers": [{"field": "groups", "value": "Iron"}]}'},
                            "finish_reason": "stop",
                        }
                    ],
                }

        client = FakeClient()
        result = client.complete_json("system", "user", max_completion_tokens=1000)

        assert result["matchers"][0]["field"] == "groups"
        assert client.payload_caps == [1000, 2000]
        assert client.usage.used_tokens == 23


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


def test_unique_candidate_verification_requires_match_and_no_missing_constraints():
    assert schema_resolver.verification_accepts_unique_candidate(
        {"matches": True, "missing_or_contradicted_constraints": []}
    ) is True
    assert schema_resolver.verification_accepts_unique_candidate(
        {"matches": False, "missing_or_contradicted_constraints": ["material unobtainium"]}
    ) is False
    assert schema_resolver.verification_accepts_unique_candidate(
        {"matches": True, "missing_or_contradicted_constraints": ["color transparent magenta plaid"]}
    ) is False


def test_unmapped_judgement_requires_unresolved_and_fatal_constraints():
    assert schema_resolver.unmapped_judgement_forces_unresolved(
        {"unresolved": True, "fatal_constraints": ["material unobtainium"]}
    ) is True
    assert schema_resolver.unmapped_judgement_forces_unresolved(
        {"unresolved": True, "fatal_constraints": []}
    ) is False
    assert schema_resolver.unmapped_judgement_forces_unresolved(
        {"unresolved": False, "fatal_constraints": ["duplicate group label"]}
    ) is False


def test_empty_accepted_values_count_as_ungrounded_constraints():
    assert schema_resolver.has_unmapped_or_ungrounded_constraints({"unmapped_constraints": ["material"]}) is True
    assert schema_resolver.has_unmapped_or_ungrounded_constraints(
        {"matchers": [{"field": "row_text", "value": "unobtainium", "accepted_values": []}]}
    ) is True
    assert schema_resolver.has_unmapped_or_ungrounded_constraints(
        {"matchers": [{"field": "groups", "value": "Spring Steel", "accepted_values": ["Spring Steel"]}]}
    ) is False


def test_broad_taxonomy_synonym_stays_ambiguous_when_not_strict():
    mapped = {
        "matchers": [{"field": "family", "value": "cartridge heater", "accepted_values": []}],
        "unmapped_constraints": [],
    }

    assert schema_resolver.broad_taxonomy_request_should_remain_ambiguous("cartridge heater", mapped) is True
    assert schema_resolver.broad_taxonomy_request_should_remain_ambiguous(
        "Required exact constraint: material must be unobtainium.", mapped
    ) is False
    assert schema_resolver.broad_taxonomy_request_should_remain_ambiguous('cartridge heater 1/2" diameter', mapped) is False


def test_broad_family_only_request_stays_ambiguous_after_accidental_unique_filter():
    rows = [
        {"part_number": "2498N11", "family": "Welding Clamps", "groups": ["Fixed Jaw"], "attributes": {}},
        {"part_number": "2498N12", "family": "Welding Clamps", "groups": ["Pivoting Jaw"], "attributes": {}},
        {"part_number": "2498N13", "family": "Self-Adjusting Locking Pliers Clamps", "groups": ["Fixed Jaw"], "attributes": {}},
    ]
    matches = [rows[0]]
    mapped = {
        "matchers": [
            {
                "constraint": "description",
                "field": "family",
                "value": "welding clamps",
                "accepted_values": ["welding clamps"],
            }
        ],
        "unmapped_constraints": [],
    }

    widened, returned_parts, trace = schema_resolver.maybe_preserve_broad_ambiguity(
        description="welding clamp",
        mapped=mapped,
        rows=rows,
        matches=matches,
    )

    assert returned_parts == ["2498N11", "2498N12", "2498N13"]
    assert widened == rows
    assert trace is not None
    assert trace["field"] == "metadata.ambiguity_guard"
    assert trace["after_unique_parts"] == 3


def test_specific_or_ungrounded_constraints_do_not_use_broad_ambiguity_guard():
    broad_mapped = {
        "matchers": [{"field": "family", "value": "welding clamps", "accepted_values": ["welding clamps"]}],
        "unmapped_constraints": [],
    }
    specific_mapped = {
        "matchers": [
            {"field": "family", "value": "welding clamps", "accepted_values": ["welding clamps"]},
            {"field": "attributes.Opening Max.", "value": '2-1/4"', "accepted_values": ['2-1/4"']},
        ],
        "unmapped_constraints": [],
    }
    ungrounded_mapped = {
        "matchers": [
            {"field": "family", "value": "welding clamps", "accepted_values": ["welding clamps"]},
            {"field": "attributes.Material", "value": "unobtainium", "accepted_values": []},
        ],
        "unmapped_constraints": [],
    }

    assert schema_resolver.broad_request_should_remain_ambiguous("welding clamp", broad_mapped) is True
    assert schema_resolver.broad_request_should_remain_ambiguous('welding clamp with 2-1/4" opening', broad_mapped) is False
    assert schema_resolver.broad_request_should_remain_ambiguous("welding clamp with maximum opening", specific_mapped) is False
    assert schema_resolver.broad_request_should_remain_ambiguous("welding clamp made of unobtainium", ungrounded_mapped) is False
