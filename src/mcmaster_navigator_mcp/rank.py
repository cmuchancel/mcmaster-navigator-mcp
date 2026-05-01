from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .extract import PART_RE, clean_text
from .models import ProductHit


UNIT_TERM_RE = re.compile(
    r"\d(?:[\d./-]*\d)?(?:amp|degree|feet|foot|ft|hp|inch|lb|lbs|mm|oz|psi|rpm|volt|watt)\b"
)
FIELD_RE = re.compile(r"(?:^|[.;]\s*)([A-Za-z][A-Za-z0-9 ./'&()#-]{1,70}?):\s*([^;]+)")
MODEL_CODE_RE = re.compile(r"(?=.*\d)(?:[a-z]+\d|\d+[a-z])[a-z0-9-]{3,}|\d{5,}")

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "into",
    "inch",
    "inches",
    "is",
    "it",
    "mcmaster",
    "of",
    "on",
    "or",
    "part",
    "product",
    "the",
    "this",
    "to",
    "with",
    "x",
}

QUERY_STOPWORDS = STOPWORDS | {
    "black",
    "blue",
    "coated",
    "degree",
    "female",
    "fully",
    "male",
    "mm",
    "no",
    "pack",
    "pkg",
    "polished",
    "psi",
    "qty",
    "series",
    "steel",
    "threaded",
    "white",
}


@dataclass
class RankedPart:
    product: ProductHit
    score: float
    matched_terms: list[str]
    missing_terms: list[str]
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "part_number": self.product.part_number,
            "name": self.product.name,
            "url": self.product.url,
            "score": self.score,
            "matched_terms": self.matched_terms,
            "missing_terms": self.missing_terms,
            "evidence": self.evidence,
            "sources": self.product.sources,
            "confidence": self.product.confidence,
        }


def derive_search_query(description: str) -> str:
    description = strip_part_numbers(description)
    segments = split_description_segments(description)
    segment_queries: list[tuple[str, list[str], int, int]] = []
    for segment in segments:
        query = query_like(segment)
        if not query:
            continue
        tokens = query.split()
        alpha_count = sum(1 for token in tokens if re.search(r"[a-zA-Z]", token))
        numeric_count = sum(1 for token in tokens if re.search(r"\d", token))
        segment_queries.append((query, tokens, alpha_count, numeric_count))

    if len(segment_queries) >= 2:
        first_query, first_tokens, first_alpha, first_numeric = segment_queries[0]
        if first_alpha <= 3 and first_numeric <= 1:
            for _query, tokens, alpha_count, numeric_count in segment_queries[1:2]:
                if 2 <= alpha_count <= 8 and numeric_count <= 1:
                    combined_tokens = list(dict.fromkeys([*first_tokens, *tokens]))
                    return " ".join(combined_tokens[:10])

    for query, tokens, alpha_count, numeric_count in segment_queries[:3]:
        if 2 <= alpha_count <= 6 and numeric_count == 0:
            return query
        if 2 <= alpha_count <= 5 and numeric_count == 1 and len(tokens) <= 5:
            return query

    candidates: list[tuple[int, int, int, str]] = []
    for index, (query, tokens, alpha_count, numeric_count) in enumerate(segment_queries):
        if alpha_count < 2:
            continue
        score = alpha_count * 3 - numeric_count * 4 - abs(len(tokens) - 5) - index
        candidates.append((-score, index, len(tokens), query))
    if candidates:
        candidates.sort()
        return candidates[0][3]

    tokens = [
        token
        for token in tokenize(description)
        if token not in QUERY_STOPWORDS and re.search(r"[a-zA-Z]", token)
    ]
    return " ".join(tokens[:8]) or clean_text(description)[:80]


def derive_search_queries(description: str, *, limit: int = 5) -> list[str]:
    queries = [derive_search_query(description)]
    segments = split_description_segments(strip_part_numbers(description))
    for segment in segments[:4]:
        query = query_like(segment)
        if not query:
            continue
        for candidate in accessory_query_variants(query):
            tokens = candidate.split()
            alpha_count = sum(1 for token in tokens if re.search(r"[a-zA-Z]", token))
            if alpha_count < 2:
                continue
            if candidate not in queries:
                queries.append(candidate)
            if len(queries) >= limit:
                break
        if len(queries) >= limit:
            break
    return queries[:limit]


def rank_products(description: str, products: list[ProductHit], *, page_title: str = "") -> list[RankedPart]:
    terms = important_terms(specific_description(description))
    ranked = [rank_product(description, terms, product, page_title=page_title) for product in products]
    return sorted(ranked, key=lambda item: (-item.score, item.product.part_number))


def rank_product(
    description: str,
    terms: list[str],
    product: ProductHit,
    *,
    page_title: str = "",
) -> RankedPart:
    evidence = best_evidence(product, page_title)
    evidence_norm = normalize(evidence)
    specific_text = specific_description(description)
    title_text = title_description(description)
    specific_terms = important_terms(specific_text)
    specific_norm = normalize(specific_text)
    title_terms = important_terms(title_text)
    title_core_terms = important_terms(title_core_description(title_text))
    title_description_norm = normalize(title_text)
    matched: list[str] = []
    missing: list[str] = []
    score = 0.0
    possible = 0.0
    for term in terms:
        weight = term_weight(term)
        possible += weight
        if term_matches(term, evidence_norm):
            matched.append(term)
            score += weight
        else:
            missing.append(term)
    if product.name:
        score += 0.4
        possible += 0.4
    if product.context:
        score += 0.4
        possible += 0.4
    if product.name and "product_page" in product.sources:
        title_norm = normalize(product.name)
        for term in title_terms:
            weight = term_weight(term) * 4.0
            possible += weight
            if term_matches(term, title_norm):
                score += weight
        for term in important_terms(product.name):
            weight = term_weight(term) * 1.4
            possible += weight
            if term_matches(term, title_description_norm):
                score += weight
    field_score, field_possible = field_match_score(description, evidence)
    score += field_score
    possible += field_possible
    negation_possible = negation_conflict_score(description, evidence)
    possible += negation_possible
    if product.confidence >= 0.9:
        score += 0.2
        possible += 0.2
    normalized = round(score / possible, 4) if possible else 0.0
    if product.name and "product_page" in product.sources and title_core_terms:
        title_norm = normalize(product.name)
        missing_core = sum(1 for term in title_core_terms if not term_matches(term, title_norm))
        if missing_core:
            normalized = round(normalized * max(0.55, 1.0 - 0.15 * missing_core), 4)
    if "link" not in product.sources:
        normalized = round(normalized * 0.55, 4)
    return RankedPart(
        product=product,
        score=normalized,
        matched_terms=matched[:60],
        missing_terms=missing[:60],
        evidence=evidence[:900],
    )


def important_terms(description: str) -> list[str]:
    text = strip_part_numbers(description)
    raw_tokens = tokenize(text)
    terms: list[str] = []
    seen = set()
    for token in raw_tokens:
        if token in STOPWORDS:
            continue
        if len(token) < 3 and not re.search(r"\d", token):
            continue
        if token not in seen:
            terms.append(token)
            seen.add(token)
    return terms


def field_match_score(description: str, evidence: str) -> tuple[float, float]:
    specific = specific_description(description)
    description_norm = normalize(specific)
    description_fields = parse_labeled_fields(specific)
    total_score = 0.0
    total_possible = 0.0
    for segment in evidence.split(";"):
        if ":" not in segment:
            continue
        label, value = segment.split(":", 1)
        label_terms = [
            token
            for token in tokenize(label)
            if token not in STOPWORDS and len(token) > 1
        ]
        if not label_terms:
            continue
        value_terms = [
            token
            for token in tokenize(value)
            if token not in STOPWORDS and len(token) > 1
        ]
        if not value_terms:
            continue
        label_key = " ".join(label_terms)
        if label_key in description_fields:
            field_score, field_possible = best_scoped_field_score(
                description_fields[label_key],
                normalize(value),
            )
            total_score += field_score
            total_possible += field_possible
            continue
        if label_key in {"group", "selected option"}:
            value_factor = 2.6 if label_key == "selected option" else 2.1
            for token in value_terms:
                weight = term_weight(token) * value_factor
                total_possible += weight
                if term_matches(token, description_norm):
                    total_score += weight
            continue
        label_hits = sum(1 for token in label_terms if term_matches(token, description_norm))
        if label_hits == 0:
            continue
        label_factor = min(1.0, label_hits / max(len(label_terms), 1))
        for token in value_terms:
            weight = term_weight(token) * 1.8 * label_factor
            total_possible += weight
            if term_matches(token, description_norm):
                total_score += weight
    return total_score, total_possible


def parse_labeled_fields(text: str) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for match in FIELD_RE.finditer(text):
        label = clean_text(match.group(1)).strip(" .:-")
        value = clean_text(match.group(2)).strip(" .:-")
        key = field_label_key(label)
        value_norm = normalize(value)
        if not key or not value_norm:
            continue
        fields.setdefault(key, []).append(value_norm)
    return fields


def field_label_key(label: str) -> str:
    return " ".join(
        token
        for token in tokenize(label)
        if token not in STOPWORDS and len(token) > 1
    )


def best_scoped_field_score(description_values: list[str], evidence_value_norm: str) -> tuple[float, float]:
    best_score = 0.0
    best_possible = 0.0
    for description_value in description_values:
        score = 0.0
        possible = 0.0
        value_terms = [
            token
            for token in tokenize(description_value)
            if token not in STOPWORDS and len(token) > 1
        ]
        for token in value_terms:
            weight = term_weight(token) * 3.4
            possible += weight
            if term_matches(token, evidence_value_norm):
                score += weight
        if possible and (score / possible, score) > (
            (best_score / best_possible) if best_possible else -1.0,
            best_score,
        ):
            best_score = score
            best_possible = possible
    return best_score, best_possible


def negation_conflict_score(description: str, evidence: str) -> float:
    evidence_norm = normalize(evidence)
    penalty = 0.0
    for phrase in re.findall(r"\bwith\s+([^,.;]+)", specific_description(description), flags=re.IGNORECASE):
        phrase_norm = normalize(phrase)
        if not phrase_norm:
            continue
        if f" without {phrase_norm} " in f" {evidence_norm} " or f" no {phrase_norm} " in f" {evidence_norm} ":
            penalty += sum(term_weight(term) for term in tokenize(phrase_norm)) * 2.0
    return penalty


def term_weight(term: str) -> float:
    if UNIT_TERM_RE.fullmatch(term):
        return 8.0
    if MODEL_CODE_RE.fullmatch(term):
        return 8.0
    if re.search(r"\d", term):
        return 2.0
    if len(term) >= 8:
        return 1.4
    return 1.0


def term_matches(term: str, normalized_evidence: str) -> bool:
    if not term:
        return False
    if f" {term} " in f" {normalized_evidence} ":
        return True
    if re.search(r"\d", term):
        return False
    for variant in plural_variants(term):
        if f" {variant} " in f" {normalized_evidence} ":
            return True
    return False


def plural_variants(term: str) -> list[str]:
    variants = []
    if term.endswith("ies") and len(term) > 4:
        variants.append(f"{term[:-3]}y")
    if term.endswith("s") and len(term) > 3:
        variants.append(term[:-1])
    else:
        variants.append(f"{term}s")
    return variants


def best_evidence(product: ProductHit, page_title: str = "") -> str:
    pieces = [product.name, product.context]
    cleaned: list[str] = []
    seen = set()
    for piece in pieces:
        text = clean_text(piece)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        cleaned.append(text)
        seen.add(key)
    return ". ".join(cleaned)


def strip_part_numbers(value: str) -> str:
    return PART_RE.sub(" ", value or "")


def specific_description(description: str) -> str:
    text = strip_part_numbers(description)
    segments = [clean_text(segment) for segment in re.split(r"\n|(?<!\d)\.(?!\d)", text)]
    if len(segments) >= 2 and len(segments[1].split()) >= 3:
        return ". ".join(segment for segment in segments[1:] if segment)
    return text


def title_description(description: str) -> str:
    text = specific_description(description)
    field_match = FIELD_RE.search(text)
    if field_match:
        text = text[: field_match.start()]
    segments = [
        clean_text(segment)
        for segment in re.split(r";|(?<!\d)\.(?!\d)", text)
        if clean_text(segment)
    ]
    return segments[0] if segments else text


def title_core_description(title: str) -> str:
    text = clean_text(title)
    text = re.split(r",\s+with\b|\bwith\b|\bfor\b", text, maxsplit=1, flags=re.IGNORECASE)[0]
    digit_match = re.search(r"\d", text)
    if digit_match:
        text = text[: digit_match.start()]
    return clean_text(text)


def split_description_segments(description: str) -> list[str]:
    return [
        clean_text(segment)
        for segment in re.split(r"\n|,|(?<!\d)[.;:|](?!\d)", description)
    ]


def query_like(segment: str) -> str:
    text = normalize(segment)
    text = re.sub(
        r"\b(find|looking|need|want|please|show|get|select|pick|exact|single)\b",
        " ",
        text,
    )
    tokens = [
        normalize_token(token)
        for token in text.split()
    ]
    tokens = [
        token
        for token in tokens
        if token and token not in QUERY_STOPWORDS and not (token.isdigit() and len(token) < 4)
    ]
    return " ".join(tokens[:10])


def accessory_query_variants(query: str) -> list[str]:
    tokens = query.split()
    if not tokens:
        return []
    variants = []
    dehyphenated = dehyphen_alpha_words(query)
    for candidate in (dehyphenated, query):
        if candidate and candidate not in variants:
            variants.append(candidate)
    if tokens[0] in {"adapter", "bracket", "cable", "case", "cord", "cover", "guard", "holder", "mount"} and len(tokens) >= 3:
        rest = tokens[1:]
        singular_rest = [
            token[:-1] if token.endswith("s") and len(token) > 3 else token
            for token in rest
        ]
        variants.append(" ".join([*singular_rest, tokens[0]]))
        if len(singular_rest) >= 1:
            variants.append(" ".join([singular_rest[-1], tokens[0]]))
    if "l-key" in query or re.search(r"\bl\s+key\b", dehyphenated):
        l_key_variants = ["l keys"]
        if "hex" in dehyphenated.split():
            l_key_variants = ["hex l-key", "l keys", "hex keys", *l_key_variants]
        for candidate in l_key_variants:
            if candidate not in variants:
                variants.append(candidate)
    if "compression spring" in dehyphenated:
        spring_variants = ["compression springs"]
        if "music wire" in dehyphenated:
            spring_variants.append("music wire springs")
        if "spring steel" in dehyphenated:
            spring_variants.append("spring steel compression springs")
        for candidate in reversed(spring_variants):
            if candidate in variants:
                variants.remove(candidate)
            variants.insert(0, candidate)
    return variants


def dehyphen_alpha_words(query: str) -> str:
    return re.sub(r"(?<=[a-zA-Z])-(?=[a-zA-Z])", " ", query)


def tokenize(value: str) -> list[str]:
    return [
        token
        for token in (normalize_token(token) for token in normalize(value).split())
        if token
    ]


def normalize(value: str) -> str:
    text = (value or "").lower()
    replacements = {
        "dia.": "diameter",
        "ht.": "height",
        "lg.": "long",
        "o'all": "overall",
        "qty.": "quantity",
        "wd.": "width",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = text.replace("\u00d7", " x ")
    text = text.replace("\u2014", " ")
    text = text.replace("\u2013", " ")
    text = text.replace("\u2011", "-")
    text = text.replace("'", " ")
    text = text.replace('"', " inch ")
    text = re.sub(r"(\d+)\s*-\s*(\d+/\d+)", r"\1 \2", text)
    text = re.sub(r"\b(\d+(?:\.\d+)?)\s*a\b", r"\1 amp", text)
    text = re.sub(r"\b(\d+(?:\.\d+)?)\s*v\b", r"\1 volt", text)
    text = re.sub(r"\b(\d+(?:\.\d+)?)\s*w\b", r"\1 watt", text)
    text = re.sub(r"\bw\b", "watt", text)
    text = re.sub(r"(\d+)\s*/\s*(\d+)", r"\1/\2", text)
    unit = r"(amp|degree|feet|foot|ft|hp|inch|lb|lbs|mm|oz|psi|rpm|volt|watt)"
    text = re.sub(
        rf"\b(\d+)\s+(\d+/\d+)\s+{unit}\b",
        r"\1 \2 \3 \1-\2\3",
        text,
    )
    text = re.sub(
        rf"\b(\d+(?:\.\d+)?|\d+/\d+)\s+{unit}\b",
        r"\1 \2 \1\2",
        text,
    )
    text = re.sub(r"[^a-z0-9/.-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_token(token: str) -> str:
    if re.fullmatch(r"\d+\.\d+", token):
        return token
    return token.strip(".-")
