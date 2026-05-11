from __future__ import annotations

import re

from .extract import PART_RE, clean_text


QUERY_BASE_STOPWORDS = {
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

QUERY_STOPWORDS = QUERY_BASE_STOPWORDS | {
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


def strip_part_numbers(value: str) -> str:
    return PART_RE.sub(" ", value or "")


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
    tokens = [normalize_token(token) for token in text.split()]
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
