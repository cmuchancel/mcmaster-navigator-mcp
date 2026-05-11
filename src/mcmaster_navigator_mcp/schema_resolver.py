from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import unquote, urlparse
from typing import Any

from .extract import PART_RE, clean_text
from .models import PageSnapshot
from .navigator import McMasterNavigator, _rank_links_for_query
from .rank import derive_search_queries, normalize, term_matches

LITERAL_IDENTIFIER_RE = re.compile(
    r"\b(?=[A-Z0-9./-]{5,}\b)(?=[A-Z0-9./-]*\d)"
    r"(?:[A-Z]+\d[A-Z0-9./-]*|\d+[A-Z][A-Z0-9./-]*|\d+(?:[-/][A-Z0-9]+)+|[A-Z0-9]+(?:[-/][A-Z0-9]+)+)\b",
    re.IGNORECASE,
)
STRICT_REQUIREMENT_RE = re.compile(r"\b(required|must|shall|exact constraint|exact requirement|only return)\b", re.IGNORECASE)


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
            raise BudgetExceeded(f"next estimated call would exceed token budget ({self.used_tokens}+{estimated_tokens}>{self.limit})")
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
        completion_cap = max_completion_tokens
        last_error = ""
        for attempt in range(3):
            estimated = estimate_tokens(system) + estimate_tokens(user) + completion_cap
            self.budget.reserve(estimated)
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
                "max_completion_tokens": completion_cap,
            }
            data = self._request_completion(payload)
            usage = data.get("usage") or {}
            total_tokens = usage.get("total_tokens")
            if not isinstance(total_tokens, int):
                total_tokens = estimated
            self.budget.record(total_tokens)
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish_reason = clean_text(str(choice.get("finish_reason") or ""))
            try:
                return json.loads(content)
            except json.JSONDecodeError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt >= 2:
                    raise RuntimeError(f"OpenAI returned invalid JSON after retries: {last_error}") from exc
            if finish_reason == "length" or "Unterminated string" in last_error:
                completion_cap = min(max(completion_cap * 2, completion_cap + 800), 5000)
            else:
                completion_cap = min(completion_cap + 800, 5000)
            time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"OpenAI returned invalid JSON after retries: {last_error}")

    def _request_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        active_payload = dict(payload)
        for attempt in range(3):
            try:
                return self._post_completion(active_payload)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if "max_completion_tokens" in body and exc.code == 400 and "max_completion_tokens" in active_payload:
                    active_payload = dict(active_payload)
                    active_payload.pop("max_completion_tokens", None)
                    continue
                if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise RuntimeError(f"OpenAI API error {exc.code}: {body[:800]}") from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise RuntimeError(f"OpenAI API request failed: {exc}") from exc
        raise RuntimeError("OpenAI API request failed after retries")

    def _post_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
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


def resolve_exact_part_dynamic(
    navigator: McMasterNavigator,
    description: str,
    *,
    search_query: str | None = None,
    max_candidates: int = 10,
    max_pages: int = 8,
    auto_drill_depth: int | None = None,
) -> dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for dynamic schema resolution")
    model = os.environ.get("MCMASTER_NAV_LLM_MODEL") or os.environ.get("FUSION_LLM_MODEL") or "gpt-5.4-mini"
    token_budget = TokenBudget(int(os.environ.get("MCMASTER_NAV_LLM_TOKEN_BUDGET", "2500000")))
    client = OpenAIJsonClient(api_key=api_key, model=model, budget=token_budget)
    started = time.time()
    llm_payloads: dict[str, Any] = {}
    pages: list[dict[str, Any]] = []
    filter_trace: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    status = "error"
    error = ""
    selected_part_number = None
    returned_parts: list[str] = []

    try:
        normalized = llm_extract_search_and_constraints(client, description)
        llm_payloads["normalized"] = normalized
        max_searches = int(os.environ.get("MCMASTER_NAV_LLM_MAX_SEARCHES", "2"))
        if search_query:
            search_queries = [search_query]
            for query in schema_search_queries(description, normalized, limit=max_searches):
                if query.lower() not in {item.lower() for item in search_queries}:
                    search_queries.append(query)
            search_queries = search_queries[:max_searches]
        else:
            search_queries = schema_search_queries(description, normalized, limit=max_searches)
        if not search_queries:
            search_queries = derive_search_queries(description, limit=max_searches)

        rows, pages = collect_schema_rows(
            navigator,
            description=description,
            search_queries=search_queries,
            max_pages=max_pages,
            auto_drill_depth=auto_drill_depth,
            max_rows=int(os.environ.get("MCMASTER_NAV_LLM_MAX_ROWS", "700")),
        )
        field_summary = summarize_dynamic_fields(rows, max_values=int(os.environ.get("MCMASTER_NAV_LLM_MAX_FIELD_VALUES", "160")))
        llm_payloads["field_summary"] = field_summary
        mapped = llm_map_constraints_to_schema(
            client,
            description=description,
            normalized=normalized,
            field_summary=field_summary,
        )
        value_normalization = llm_normalize_matcher_values(
            client,
            description=description,
            matchers=mapped.get("matchers", []),
            rows=rows,
            max_values=int(os.environ.get("MCMASTER_NAV_LLM_MAX_FIELD_VALUES", "160")),
        )
        mapped["matchers"] = value_normalization.get("matchers", mapped.get("matchers", []))
        mapped["matchers"] = apply_explicit_label_values(description, mapped.get("matchers", []), rows)
        mapped["matchers"] = augment_literal_identifier_matchers(description, mapped.get("matchers", []), rows)
        llm_payloads["mapped"] = mapped
        llm_payloads["value_normalization"] = value_normalization
        matches, filter_trace = apply_constraint_matchers(rows, mapped.get("matchers", []))
        matches, variant_trace = apply_option_variant_scope(matches, mapped.get("matchers", []))
        if variant_trace:
            filter_trace.append(variant_trace)

        if should_repair_matchers(mapped.get("matchers", []), filter_trace, matches):
            llm_payloads["initial_filter_trace"] = filter_trace
            repair = llm_repair_matchers_from_live_schema(
                client,
                description=description,
                normalized=normalized,
                matchers=mapped.get("matchers", []),
                field_summary=field_summary,
                rows=rows,
                filter_trace=filter_trace,
                max_values=int(os.environ.get("MCMASTER_NAV_LLM_MAX_FIELD_VALUES", "160")),
            )
            llm_payloads["repair"] = repair
            repaired_matchers = repair.get("matchers", [])
            if repaired_matchers:
                repaired_matchers = apply_explicit_label_values(description, repaired_matchers, rows)
                repaired_matchers = augment_literal_identifier_matchers(description, repaired_matchers, rows)
                mapped["matchers"] = repaired_matchers
                llm_payloads["mapped"] = mapped
                matches, filter_trace = apply_constraint_matchers(rows, repaired_matchers)
                matches, variant_trace = apply_option_variant_scope(matches, repaired_matchers)
                if variant_trace:
                    filter_trace.append(variant_trace)

        matches, returned_parts, ambiguity_trace = maybe_preserve_broad_ambiguity(
            description=description,
            mapped=mapped,
            rows=rows,
            matches=matches,
        )
        if ambiguity_trace:
            filter_trace.append(ambiguity_trace)
        forced_unresolved = False
        if (
            len(returned_parts) > 1
            and has_unmapped_or_ungrounded_constraints(mapped)
            and not broad_taxonomy_request_should_remain_ambiguous(description, mapped)
        ):
            unmapped_judgement = llm_judge_unmapped_constraints(
                client,
                description=description,
                normalized=normalized,
                mapped=mapped,
                sample_rows=matches[:12],
            )
            llm_payloads["unmapped_judgement"] = unmapped_judgement
            if unmapped_judgement_forces_unresolved(unmapped_judgement):
                filter_trace.append(
                    {
                        "constraint": "unmapped or ungrounded required constraints",
                        "field": "metadata.constraint_grounding",
                        "value": clean_text(str(unmapped_judgement.get("reason") or "")),
                        "comparator": "llm_required_constraint_grounding_check",
                        "accepted_values": [],
                        "before_unique_parts": len(returned_parts),
                        "after_unique_parts": 0,
                        "fatal_constraints": unmapped_judgement.get("fatal_constraints", []),
                    }
                )
                matches = []
                returned_parts = []
                selected_part_number = None
                status = "unresolved"
                forced_unresolved = True
        if len(returned_parts) == 1:
            status = "unique"
            selected_part_number = returned_parts[0]
            verification = llm_verify_unique_candidate(
                client,
                description=description,
                normalized=normalized,
                selected_row=matches[0],
                filter_trace=filter_trace,
            )
            llm_payloads["verification"] = verification
            if not verification_accepts_unique_candidate(verification):
                filter_trace.append(
                    {
                        "constraint": "unique candidate verification",
                        "field": "metadata.final_verification",
                        "value": clean_text(str(verification.get("reason") or "")),
                        "comparator": "llm_strict_satisfaction_check",
                        "accepted_values": [],
                        "before_unique_parts": 1,
                        "after_unique_parts": 0,
                        "missing_or_contradicted_constraints": verification.get("missing_or_contradicted_constraints", []),
                    }
                )
                matches = []
                returned_parts = []
                selected_part_number = None
                status = "unresolved"
        elif len(returned_parts) > 1:
            status = "ambiguous"
        elif not forced_unresolved:
            status = "unresolved"
    except BudgetExceeded as exc:
        status = "budget_exceeded"
        error = str(exc)
        matches = []
    except Exception as exc:
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
        matches = []

    return {
        "description": description,
        "strategy": "dynamic_schema_llm",
        "model": model,
        "status": status,
        "part_number": selected_part_number,
        "selected_part": row_result(matches[0]) if selected_part_number and matches else None,
        "returned_part_numbers": returned_parts[:max_candidates],
        "returned_count": len(returned_parts),
        "candidates": [row_result(row) for row in matches[:max_candidates]],
        "candidate_count": len(returned_parts),
        "pages_visited": pages,
        "filter_trace": filter_trace,
        "llm_payloads": llm_payloads,
        "diagnostics": {
            "max_candidates": max_candidates,
            "max_pages": max_pages,
            "auto_drill_depth": auto_drill_depth,
            "llm_usage": token_budget.to_dict(),
            "error": error,
            "seconds": round(time.time() - started, 3),
        },
    }


def collect_schema_rows(
    navigator: McMasterNavigator,
    *,
    description: str,
    search_queries: list[str],
    max_pages: int,
    auto_drill_depth: int | None,
    max_rows: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    seen_page_urls: set[str] = set()
    search_pages: list[tuple[str, PageSnapshot]] = []
    for query in search_queries:
        if len(pages) >= max_pages:
            break
        page = navigator.search(query, max_depth=auto_drill_depth)
        if page.url not in seen_page_urls:
            pages.append(page.to_summary_dict())
            seen_page_urls.add(page.url)
            search_pages.append((query, page))
        rows = merge_rows(rows, rows_from_page(page))
        if len(rows) >= max_rows:
            return rows[:max_rows], pages

    for query, page in search_pages:
        if len(rows) >= max_rows:
            break
        for link in rank_schema_links(page, description=description, query=query):
            if len(pages) >= max_pages or len(rows) >= max_rows:
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
    return rows[:max_rows], pages


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
        key = (part_number, tuple(sorted(attributes.items())), tuple(groups))
        current = merged.get(key)
        clean_row = {**row, "part_number": part_number, "attributes": attributes, "groups": groups}
        if current is None:
            merged[key] = clean_row
            continue
        for field in ("family", "selected_option", "evidence", "url", "page_url", "page_title"):
            value = clean_text(str(clean_row.get(field, "")))
            if value and len(value) > len(clean_text(str(current.get(field, "")))):
                current[field] = value
    return list(merged.values())


def summarize_dynamic_fields(rows: list[dict[str, Any]], *, max_values: int) -> dict[str, Any]:
    values = all_field_values(rows, max_values=max_values)
    counts: dict[str, int] = {}
    for row in rows:
        for field in row_fields(row):
            counts[field] = counts.get(field, 0) + 1
    return {
        "row_count": len(rows),
        "fields": [
            {"field": field, "count": counts.get(field, len(values[field])), "sample_values": values[field]}
            for field in sorted(values)
        ],
    }


def row_fields(row: dict[str, Any]) -> list[str]:
    fields = ["family", "selected_option"]
    fields.extend(["groups"] * len(row.get("groups", [])))
    attributes = row.get("attributes", {})
    if isinstance(attributes, dict):
        fields.extend(f"attributes.{clean_text(str(key))}" for key in attributes)
    return [field for field in fields if field]


def apply_constraint_matchers(rows: list[dict[str, Any]], matchers: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_rows = list(rows)
    current = list(rows)
    trace: list[dict[str, Any]] = []
    ordered_matchers = sorted(
        [matcher for matcher in matchers if isinstance(matcher, dict)],
        key=matcher_application_priority,
    )
    for raw_matcher in ordered_matchers:
        field = clean_text(str(raw_matcher.get("field", "")))
        value = clean_text(str(raw_matcher.get("value", "")))
        comparator = clean_text(str(raw_matcher.get("comparator") or "contains_all_terms"))
        accepted_values_provided = "accepted_values" in raw_matcher
        accepted_values = [clean_text(str(item)) for item in raw_matcher.get("accepted_values", []) if clean_text(str(item))]
        if not value and accepted_values:
            value = accepted_values[0]
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
    if not value and accepted_values:
        value = accepted_values[0]
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
        base_parts = {clean_text(str(part)).upper() for part in metadata.get("base_part_numbers", []) if clean_text(str(part))}
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


def unique_part_numbers(rows: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    parts: list[str] = []
    for row in rows:
        part = clean_text(str(row.get("part_number", ""))).upper()
        if part and part not in seen:
            seen.add(part)
            parts.append(part)
    return parts


def maybe_preserve_broad_ambiguity(
    *,
    description: str,
    mapped: dict[str, Any],
    rows: list[dict[str, Any]],
    matches: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any] | None]:
    returned_parts = unique_part_numbers(matches)
    broad_parts = unique_part_numbers(rows)
    if len(returned_parts) != 1 or len(broad_parts) <= 1:
        return matches, returned_parts, None
    if not broad_request_should_remain_ambiguous(description, mapped):
        return matches, returned_parts, None
    return list(rows), broad_parts, {
        "constraint": "underspecified broad request",
        "field": "metadata.ambiguity_guard",
        "value": clean_text(description),
        "comparator": "preserve_live_catalog_ambiguity",
        "accepted_values": [],
        "before_unique_parts": 1,
        "after_unique_parts": len(broad_parts),
        "matched_fields": sorted(
            {
                clean_text(str(matcher.get("field", "")))
                for matcher in mapped.get("matchers", [])
                if isinstance(matcher, dict) and clean_text(str(matcher.get("field", "")))
            }
        ),
        "candidate_part_numbers": broad_parts[:20],
    }


def broad_request_should_remain_ambiguous(description: str, mapped: dict[str, Any]) -> bool:
    if LITERAL_IDENTIFIER_RE.search(description) or re.search(r"\d", description):
        return False
    if mapped.get("unmapped_constraints"):
        return False
    matchers = [matcher for matcher in mapped.get("matchers", []) if isinstance(matcher, dict)]
    if not matchers:
        return False
    broad_fields = {"family", "groups", "row_text"}
    for matcher in matchers:
        field = clean_text(str(matcher.get("field", "")))
        if field not in broad_fields:
            return False
        values = [clean_text(str(matcher.get("value", "")))]
        if "accepted_values" in matcher:
            accepted_values = [clean_text(str(value)) for value in matcher.get("accepted_values", []) if clean_text(str(value))]
            if not accepted_values:
                return False
            values.extend(accepted_values)
        if any(LITERAL_IDENTIFIER_RE.search(value) or re.search(r"\d", value) for value in values):
            return False
    return True


def has_unmapped_or_ungrounded_constraints(mapped: dict[str, Any]) -> bool:
    if mapped.get("unmapped_constraints"):
        return True
    for matcher in mapped.get("matchers", []):
        if not isinstance(matcher, dict):
            continue
        if "accepted_values" not in matcher:
            continue
        accepted_values = [clean_text(str(value)) for value in matcher.get("accepted_values", []) if clean_text(str(value))]
        if not accepted_values:
            return True
    return False


def broad_taxonomy_request_should_remain_ambiguous(description: str, mapped: dict[str, Any]) -> bool:
    if STRICT_REQUIREMENT_RE.search(description) or LITERAL_IDENTIFIER_RE.search(description) or re.search(r"\d", description):
        return False
    if mapped.get("unmapped_constraints"):
        return False
    matchers = [matcher for matcher in mapped.get("matchers", []) if isinstance(matcher, dict)]
    if not matchers:
        return True
    taxonomy_fields = {"family", "groups", "row_text"}
    for matcher in matchers:
        field = clean_text(str(matcher.get("field", "")))
        if field not in taxonomy_fields:
            return False
    return True


def row_result(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "part_number": row.get("part_number"),
        "url": row.get("url"),
        "family": row.get("family"),
        "groups": row.get("groups", []),
        "selected_option": row.get("selected_option", ""),
        "attributes": row.get("attributes", {}),
        "evidence": row.get("evidence", ""),
        "source": row.get("source", ""),
    }


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
                "constraints": [{"constraint": "literal requested requirement", "value": "value to match on a catalog row", "required": True}],
            },
            "rules": [
                "The first search query must be the broad product family, not a fully specified part.",
                "Prefer product-family search queries such as socket head screw, compression spring, drawer slide.",
                "Omit dimensions, ratings, materials, counts, finishes, and option values from search queries unless they are part of the product-family noun.",
                "If the description explicitly says Family: X, include X as a search query.",
                "Constraints should contain only requirements present in the description.",
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
                    {"constraint": "requested requirement", "field": "one of available_fields or row_text", "value": "literal value", "comparator": "contains_all_terms"}
                ],
                "unmapped_constraints": ["constraint text if no available field can test it"],
            },
            "rules": [
                "Use groups for values that appear as table group headings.",
                "Use attributes.<column name> for values that appear under a specific dynamic table column.",
                "Use family only for the broad product family.",
                "Use row_text only if no specific field can represent the constraint.",
                "Do not output a matcher for a constraint unless the field summary shows the field exists.",
            ],
        },
        ensure_ascii=True,
    )
    result = client.complete_json(system, user, max_completion_tokens=1800)
    if not isinstance(result, dict):
        raise RuntimeError("LLM mapper returned non-object JSON")
    return sanitize_matchers(result, allowed_fields)


def sanitize_matchers(result: dict[str, Any], allowed_fields: list[str]) -> dict[str, Any]:
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
            "output_schema": {"matchers": [{"constraint": "same", "field": "same", "value": "same", "comparator": "same", "accepted_values": ["exact strings copied from field_values[field]"]}]},
            "rules": [
                "accepted_values must be copied exactly from field_values for that field.",
                "Use the full description to resolve aliases, units, abbreviations, and product-family wording.",
                "If no supplied value satisfies the constraint, use an empty accepted_values list.",
            ],
        },
        ensure_ascii=True,
    )
    result = client.complete_json(system, user, max_completion_tokens=1600)
    if not isinstance(result, dict):
        raise RuntimeError("LLM value normalizer returned non-object JSON")
    return normalize_matcher_output(result, clean_matchers, field_values)


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
                "matchers": [{"constraint": "requested requirement", "field": "one key from field_values", "value": "literal requested value", "comparator": "equals_normalized", "accepted_values": ["exact strings copied from field_values[field]"]}],
                "untestable_constraints": ["required description constraint not represented by supplied field values"],
            },
            "rules": [
                "Every accepted_values item must be copied exactly from field_values for the chosen field.",
                "Do not output a matcher unless accepted_values is non-empty.",
                "Do not choose close-looking values that contradict a requested dimension, rating, material, count, or option.",
                "If a required constraint cannot be grounded in the supplied values, put it in untestable_constraints.",
            ],
        },
        ensure_ascii=True,
    )
    result = client.complete_json(system, user, max_completion_tokens=2200)
    if not isinstance(result, dict):
        raise RuntimeError("LLM repair returned non-object JSON")
    return repair_matcher_output(result, field_values)


def llm_verify_unique_candidate(
    client: OpenAIJsonClient,
    *,
    description: str,
    normalized: dict[str, Any],
    selected_row: dict[str, Any],
    filter_trace: list[dict[str, Any]],
) -> dict[str, Any]:
    system = (
        "You are a strict verifier for supplier catalog part matching. "
        "Given a user part description and one live catalog row, decide whether the row satisfies every required constraint. "
        "If any required constraint is absent, contradicted, impossible, or only matched by ignoring an ungrounded value, return matches=false. "
        "Do not infer from outside the supplied row. Return only JSON."
    )
    user = json.dumps(
        {
            "task": "Verify whether the selected live catalog row satisfies the complete requested description.",
            "description": description,
            "normalized_constraints": normalized.get("constraints", []),
            "selected_row": row_result(selected_row),
            "filter_trace": filter_trace,
            "output_schema": {
                "matches": True,
                "missing_or_contradicted_constraints": ["required constraint not satisfied by selected_row"],
                "reason": "short explanation",
            },
            "rules": [
                "Treat constraints marked required=true as mandatory.",
                "If the description says only return a part when every required exact constraint is present, enforce that literally.",
                "A row does not satisfy a requested material, color, dimension, rating, option, or count unless that value appears in the selected row or is a clear unit/name equivalent.",
                "Skipped matchers with empty accepted_values are evidence that a requested value was not grounded; reject if that value is required by the description.",
                "For underspecified descriptions, do not reject just because many other rows may also match; this verifier only checks whether this one row satisfies the stated constraints.",
            ],
        },
        ensure_ascii=True,
    )
    result = client.complete_json(system, user, max_completion_tokens=1000)
    if not isinstance(result, dict):
        raise RuntimeError("LLM verifier returned non-object JSON")
    missing = [
        clean_text(str(item))
        for item in result.get("missing_or_contradicted_constraints", [])
        if clean_text(str(item))
    ]
    return {
        "matches": bool(result.get("matches")),
        "missing_or_contradicted_constraints": missing,
        "reason": clean_text(str(result.get("reason") or "")),
    }


def verification_accepts_unique_candidate(verification: dict[str, Any]) -> bool:
    return bool(verification.get("matches")) and not verification.get("missing_or_contradicted_constraints")


def llm_judge_unmapped_constraints(
    client: OpenAIJsonClient,
    *,
    description: str,
    normalized: dict[str, Any],
    mapped: dict[str, Any],
    sample_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    system = (
        "You are a strict catalog matching judge. A resolver found multiple live catalog candidates, "
        "but some requested constraints were not mapped to live schema fields or had no grounded live values. "
        "Decide whether those ungrounded constraints are mandatory requirements that make every candidate unresolved. "
        "Return only JSON."
    )
    ungrounded_matchers = [
        matcher
        for matcher in mapped.get("matchers", [])
        if isinstance(matcher, dict)
        and "accepted_values" in matcher
        and not [clean_text(str(value)) for value in matcher.get("accepted_values", []) if clean_text(str(value))]
    ]
    user = json.dumps(
        {
            "task": "Decide whether unmapped or ungrounded constraints should force status=unresolved instead of ambiguous.",
            "description": description,
            "normalized_constraints": normalized.get("constraints", []),
            "mapped_matchers": mapped.get("matchers", []),
            "unmapped_constraints": mapped.get("unmapped_constraints", []),
            "ungrounded_matchers": ungrounded_matchers,
            "sample_candidate_rows": [row_result(row) for row in sample_rows],
            "output_schema": {
                "unresolved": True,
                "fatal_constraints": ["mandatory requested constraint not grounded or contradicted by live rows"],
                "reason": "short explanation",
            },
            "rules": [
                "Return unresolved=true only when an unmapped or ungrounded constraint is a mandatory user requirement and no supplied row shows that requirement.",
                "An ungrounded matcher with empty accepted_values is evidence that the requested value was absent from live field values.",
                "Return unresolved=false when the unmapped or ungrounded item is merely a duplicate family/group label, optional context, a value already represented by other mapped_matchers, or a harmless unavailable column such as a blank spec.",
                "If the user explicitly says only to return a part when every exact requirement is present, missing required constraints are fatal.",
                "Do not reject because the query is broad; broad underspecified queries should remain ambiguous.",
            ],
        },
        ensure_ascii=True,
    )
    result = client.complete_json(system, user, max_completion_tokens=900)
    if not isinstance(result, dict):
        raise RuntimeError("LLM unmapped-constraint judge returned non-object JSON")
    fatal_constraints = [
        clean_text(str(item))
        for item in result.get("fatal_constraints", [])
        if clean_text(str(item))
    ]
    return {
        "unresolved": bool(result.get("unresolved")),
        "fatal_constraints": fatal_constraints,
        "reason": clean_text(str(result.get("reason") or "")),
    }


def unmapped_judgement_forces_unresolved(judgement: dict[str, Any]) -> bool:
    return bool(judgement.get("unresolved")) and bool(judgement.get("fatal_constraints"))


def normalize_matcher_output(result: dict[str, Any], clean_matchers: list[dict[str, Any]], field_values: dict[str, list[str]]) -> dict[str, Any]:
    allowed_by_field = {field: set(values) for field, values in field_values.items()}
    normalized_matchers = []
    by_key = {
        (clean_text(str(matcher.get("constraint", ""))), clean_text(str(matcher.get("field", ""))), clean_text(str(matcher.get("value", "")))): matcher
        for matcher in clean_matchers
    }
    for matcher in result.get("matchers", []):
        if not isinstance(matcher, dict):
            continue
        field = clean_text(str(matcher.get("field", "")))
        requested_value = clean_text(str(matcher.get("value") or matcher.get("constraint") or ""))
        accepted = [
            clean_text(str(value))
            for value in matcher.get("accepted_values", [])
            if clean_text(str(value)) in allowed_by_field.get(field, set())
            and accepted_catalog_value_is_compatible(field, requested_value, clean_text(str(value)))
        ]
        key = (clean_text(str(matcher.get("constraint", ""))), field, clean_text(str(matcher.get("value", ""))))
        base = by_key.get(key, matcher)
        normalized_value = clean_text(str(base.get("value") or matcher.get("value") or matcher.get("constraint") or (accepted[0] if accepted else "")))
        normalized_matchers.append({**base, "value": normalized_value, "accepted_values": accepted})
    if len(normalized_matchers) != len(clean_matchers):
        seen_keys = {
            (clean_text(str(matcher.get("constraint", ""))), clean_text(str(matcher.get("field", ""))), clean_text(str(matcher.get("value", ""))))
            for matcher in normalized_matchers
        }
        for matcher in clean_matchers:
            key = (clean_text(str(matcher.get("constraint", ""))), clean_text(str(matcher.get("field", ""))), clean_text(str(matcher.get("value", ""))))
            if key not in seen_keys:
                normalized_matchers.append({**matcher, "accepted_values": []})
    result["matchers"] = normalized_matchers
    return result


def repair_matcher_output(result: dict[str, Any], field_values: dict[str, list[str]]) -> dict[str, Any]:
    allowed_by_field = {field: set(values) for field, values in field_values.items()}
    repaired = []
    for matcher in result.get("matchers", []):
        if not isinstance(matcher, dict):
            continue
        field = clean_text(str(matcher.get("field", "")))
        requested_value = clean_text(str(matcher.get("value") or matcher.get("constraint") or ""))
        accepted = [
            clean_text(str(value))
            for value in matcher.get("accepted_values", [])
            if clean_text(str(value)) in allowed_by_field.get(field, set())
            and accepted_catalog_value_is_compatible(field, requested_value, clean_text(str(value)))
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
    result["untestable_constraints"] = [clean_text(str(item)) for item in result.get("untestable_constraints", []) if clean_text(str(item))]
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
        accepted = [clean_text(str(value)) for value in matcher.get("accepted_values", []) if clean_text(str(value))]
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
    updated = add_missing_explicit_context_matchers(labels, updated, field_values)
    return add_missing_explicit_selected_option_matchers(labels, updated, field_values)


def add_missing_explicit_context_matchers(
    labels: dict[str, list[str]],
    matchers: list[dict[str, Any]],
    field_values: dict[str, list[str]],
) -> list[dict[str, Any]]:
    updated = list(matchers)
    existing_keys = explicit_matcher_keys(updated)
    for label_key, field, constraint in (("family", "family", "Family"), ("group", "groups", "Group")):
        for label_value in labels.get(label_key, []):
            accepted = [
                live_value
                for live_value in field_values.get(field, [])
                if same_catalog_value(label_value, live_value)
            ]
            if not accepted:
                continue
            key = (field, tuple(accepted))
            if key in existing_keys:
                continue
            updated.append(
                {
                    "constraint": constraint,
                    "field": field,
                    "value": label_value,
                    "comparator": "equals_normalized",
                    "accepted_values": accepted,
                }
            )
            existing_keys.add(key)
    return updated


def explicit_matcher_keys(matchers: list[dict[str, Any]]) -> set[tuple[str, tuple[str, ...]]]:
    keys: set[tuple[str, tuple[str, ...]]] = set()
    for matcher in matchers:
        field = clean_text(str(matcher.get("field", "")))
        accepted = tuple(clean_text(str(value)) for value in matcher.get("accepted_values", []) if clean_text(str(value)))
        if field and accepted:
            keys.add((field, accepted))
    return keys


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


CATALOG_VALUE_ARTIFACT_MARKERS = (
    "product detail",
    "add to order",
    "delivers tomorrow",
    "delivers today",
    "select a compatible",
    "download select",
    "web price",
    "price each",
    "quantity each",
)

CATALOG_SEMANTIC_STOPWORDS = {
    "a",
    "an",
    "and",
    "by",
    "for",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
    "x",
    "amp",
    "amps",
    "cm",
    "degree",
    "degrees",
    "feet",
    "foot",
    "ft",
    "g",
    "hp",
    "inch",
    "inches",
    "kg",
    "lb",
    "lbs",
    "m",
    "mm",
    "oz",
    "psi",
    "rpm",
    "v",
    "volt",
    "volts",
    "w",
    "watt",
    "watts",
    "capacity",
    "diameter",
    "height",
    "id",
    "inside",
    "length",
    "long",
    "max",
    "maximum",
    "min",
    "minimum",
    "model",
    "no",
    "number",
    "od",
    "outside",
    "overall",
    "package",
    "pack",
    "part",
    "pkg",
    "qty",
    "quantity",
    "range",
    "rating",
    "size",
    "thread",
    "type",
    "width",
}

NULLISH_CATALOG_VALUES = {"-", "__", "—", "–", "none", "no", "n/a", "na", "not applicable"}
NULLISH_REQUEST_TOKENS = {"blank", "no", "none", "without", "less"}


def accepted_catalog_value_is_compatible(field: str, requested: str, accepted: str) -> bool:
    field = clean_text(field)
    requested = clean_text(requested)
    accepted = clean_text(accepted)
    if not accepted:
        return False
    if catalog_value_looks_like_ui_artifact(accepted):
        return False
    if catalog_value_is_nullish(accepted):
        return requested_catalog_value_allows_nullish(requested)
    if field == "family":
        return True
    requested_tokens = significant_catalog_tokens(requested)
    if not requested_tokens:
        return True
    accepted_norm = normalize(catalog_semantic_text(accepted))
    if not accepted_norm:
        return False
    return all(term_matches(token, accepted_norm) for token in requested_tokens)


def catalog_value_looks_like_ui_artifact(value: str) -> bool:
    normalized = normalize(canonical_compare_text(value))
    if any(marker in normalized for marker in CATALOG_VALUE_ARTIFACT_MARKERS):
        return True
    if len(value) > 240 and PART_RE.search(value):
        return True
    return False


def catalog_value_is_nullish(value: str) -> bool:
    return normalize(canonical_compare_text(value)) in NULLISH_CATALOG_VALUES or clean_text(value).lower() in NULLISH_CATALOG_VALUES


def requested_catalog_value_allows_nullish(value: str) -> bool:
    normalized = normalize(canonical_compare_text(value))
    if not normalized:
        return True
    tokens = set(normalized.split())
    return bool(tokens.intersection(NULLISH_REQUEST_TOKENS))


def significant_catalog_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for token in constraint_tokens(catalog_semantic_text(value)):
        if not re.search(r"[a-z]", token):
            continue
        if re.search(r"\d", token):
            continue
        if token in CATALOG_SEMANTIC_STOPWORDS:
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens


def catalog_semantic_text(value: str) -> str:
    text = canonical_compare_text(value)
    text = re.sub(r"(?<=[A-Za-z])[-/](?=[A-Za-z])", " ", text)
    return clean_text(text)


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


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 3)
