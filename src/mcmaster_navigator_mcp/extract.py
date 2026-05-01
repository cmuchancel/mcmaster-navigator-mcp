from __future__ import annotations

import re
from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup

from .models import PageLink, PageSnapshot, ProductHit


BASE_URL = "https://www.mcmaster.com"
PART_RE = re.compile(r"\b([1-9]\d{3,4}[A-Z]{1,2}\d{1,3})\b", re.IGNORECASE)
PART_URL_RE = re.compile(r"[/=]([1-9]\d{3,4}[A-Z]{1,2}\d{1,3})(?:[/?&#]|$)", re.IGNORECASE)

BROAD_CATEGORY_TEXT = {
    "abrading & polishing",
    "building & grounds",
    "electrical & lighting",
    "fabricating",
    "fastening & joining",
    "filtering",
    "flow & level control",
    "furniture & storage",
    "hand tools",
    "hardware",
    "heating & cooling",
    "lubricating",
    "material handling",
    "measuring & inspecting",
    "office supplies & signs",
    "pipe, tubing, hose & fittings",
    "plumbing & janitorial",
    "plumbing and janitorial",
    "power transmission",
    "pressure & temperature control",
    "pressure & temperate control",
    "pulling & lifting",
    "raw materials",
    "safety supplies",
    "sawing & cutting",
    "sealing",
    "shipping",
    "suspending",
}

NAV_TEXT = {
    "home",
    "locations",
    "returns",
    "careers",
    "mobile app",
    "solidworks add-in",
    "eprocurement",
    "api",
    "help",
    "settings",
    "terms and conditions",
    "privacy policy",
    "order",
    "order history",
}


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.replace("\xa0", " ").split()).strip()


def product_url(part_number: str) -> str:
    return f"{BASE_URL}/{part_number.upper()}"


def search_url(query: str) -> str:
    return f"{BASE_URL}/{quote_plus(query.strip())}"


def normalize_target(target: str, base_url: str | None = None) -> str:
    value = target.strip()
    if not value:
        raise ValueError("target is required")
    if PART_RE.fullmatch(value):
        return product_url(value)
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("/"):
        return urljoin(BASE_URL, value)
    if base_url and (value.startswith("?") or value.startswith("#")):
        return urljoin(base_url, value)
    return search_url(value)


def extract_part_numbers(text: str) -> list[str]:
    return sorted({match.upper() for match in PART_RE.findall(text or "")})


def detect_page_type(url: str, title: str, soup: BeautifulSoup) -> str:
    path = urlparse(url).path.lower()
    title_lower = title.lower()
    if path in {"", "/"} and title_lower in {"mcmaster-carr", ""}:
        return "home"
    if PART_RE.search(path):
        return "product"
    if _has_spec_table(soup):
        return "product"
    if "/products/" in path:
        return "category"
    if title and title != "McMaster-Carr":
        return "search"
    return "unknown"


def snapshot_from_html(
    html: str,
    url: str,
    title: str = "",
    *,
    max_products: int = 80,
    max_links: int = 100,
    trail: list[str] | None = None,
) -> PageSnapshot:
    soup = BeautifulSoup(html or "", "html.parser")
    page_title = clean_text(title) or _title_from_soup(soup)
    products = extract_products(soup, url, max_products=max_products)
    links = extract_links(soup, url, max_links=max_links)
    text_preview = clean_text(soup.get_text(" ", strip=True))[:1200]
    part_numbers = sorted({product.part_number for product in products} | set(extract_part_numbers(html)))
    return PageSnapshot(
        url=url,
        title=page_title,
        page_type=detect_page_type(url, page_title, soup),
        products=products,
        links=links,
        part_numbers=part_numbers[:max_products],
        text_preview=text_preview,
        trail=trail or [],
        diagnostics={
            "html_length": len(html or ""),
            "raw_part_number_count": len(extract_part_numbers(html)),
        },
    )


def extract_products(soup: BeautifulSoup, current_url: str, *, max_products: int = 80) -> list[ProductHit]:
    hits: dict[str, ProductHit] = {}

    def add(part_number: str, name: str, source: str, confidence: float, context: str = "") -> None:
        part = part_number.upper()
        label = _clean_product_name(name, part)
        product_context = _clean_product_context(context, part)
        existing = hits.get(part)
        if existing is None:
            hits[part] = ProductHit(
                part_number=part,
                name=label,
                url=product_url(part),
                context=product_context,
                sources=[source],
                confidence=confidence,
            )
            return
        if source not in existing.sources:
            existing.sources.append(source)
        if label and (not existing.name or existing.name == part or len(label) > len(existing.name)):
            existing.name = label
        if product_context and (not existing.context or len(product_context) > len(existing.context)):
            existing.context = product_context
        existing.confidence = max(existing.confidence, confidence)

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        for part in _parts_from_url(href):
            add(part, _anchor_context_name(anchor), "link", 0.95, _part_context_from_element(anchor, part))

    for image in soup.find_all("img"):
        blob = " ".join(
            value
            for value in [
                image.get("src", ""),
                image.get("srcset", ""),
                image.get("data-src", ""),
                image.get("data-srcset", ""),
            ]
            if value
        )
        label = clean_text(image.get("alt") or image.get("title") or "")
        for part in extract_part_numbers(blob):
            add(part, label, "image", 0.8, _part_context_from_element(image, part))

    for part in extract_part_numbers(str(soup)):
        if part not in hits:
            add(part, "", "html", 0.45, _best_part_context(soup, part))

    products = sorted(
        hits.values(),
        key=lambda item: (-item.confidence, item.part_number),
    )
    return products[:max_products]


def extract_links(soup: BeautifulSoup, current_url: str, *, max_links: int = 100) -> list[PageLink]:
    candidates: list[tuple[int, str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        text = clean_text(anchor.get_text(" ", strip=True))
        if not text or len(text) > 120:
            continue
        url = urljoin(current_url or BASE_URL, href)
        parsed = urlparse(url)
        if not parsed.netloc.endswith("mcmaster.com"):
            continue
        kind = classify_link(text, url)
        key = (text.lower(), url)
        if key in seen:
            continue
        seen.add(key)
        candidates.append((_link_priority(text, url, kind), text, url, kind))

    candidates.sort(key=lambda item: (item[0], item[1].lower(), item[2]))
    return [
        PageLink(index=index, text=text, url=url, kind=kind)
        for index, (_priority, text, url, kind) in enumerate(candidates[:max_links])
    ]


def classify_link(text: str, url: str) -> str:
    text_lower = text.lower()
    path = urlparse(url).path.lower()
    if _parts_from_url(url):
        return "product"
    if text_lower in NAV_TEXT:
        return "navigation"
    if "~~" in path:
        return "category"
    if "~" in path:
        return "filter"
    if path.startswith("/products/"):
        if "~" in path:
            return "filter"
        if text_lower in BROAD_CATEGORY_TEXT:
            return "catalog_category"
        return "category"
    if path in {"", "/"}:
        return "navigation"
    return "link"


def _link_priority(text: str, url: str, kind: str) -> int:
    text_lower = text.lower()
    if kind == "category" and text_lower not in BROAD_CATEGORY_TEXT:
        return 0
    if kind == "product":
        return 10
    if kind == "filter":
        return 20
    if kind == "catalog_category":
        return 70
    if kind == "navigation":
        return 80
    return 50


def _parts_from_url(url: str) -> list[str]:
    return [match.upper() for match in PART_URL_RE.findall(url or "")]


def _anchor_context_name(anchor) -> str:
    own_text = clean_text(anchor.get_text(" ", strip=True))
    if _is_good_product_name(own_text):
        return own_text
    for parent in anchor.parents:
        parent_text = clean_text(parent.get_text(" ", strip=True))
        if _is_good_product_name(parent_text):
            return parent_text[:180]
    return own_text


def _part_context_from_element(element, part_number: str) -> str:
    table_context = _table_row_context(element, part_number)
    if table_context:
        return table_context
    part = part_number.upper()
    candidates: list[tuple[int, int, str]] = []
    for distance, candidate in enumerate([element, *list(element.parents)[:8]]):
        text = clean_text(candidate.get_text(" ", strip=True))
        if not text:
            continue
        contains_part = part in text.upper()
        if contains_part and len(text) <= 900:
            candidates.append((distance, abs(len(text) - 180), text))
        elif distance > 0 and 8 <= len(text) <= 500:
            candidates.append((distance + 10, abs(len(text) - 140), text))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _table_row_context(element, part_number: str) -> str:
    row = element.find_parent("tr")
    if row is None:
        return ""
    row_text = clean_text(row.get_text(" ", strip=True))
    if part_number.upper() not in row_text.upper():
        return ""

    selected_index = _cell_index(element, row, part_number)
    cell_elements = row.find_all(["td", "th"], recursive=False)
    cell_texts = [clean_text(cell.get_text(" ", strip=True)) for cell in cell_elements]
    if not any(cell_texts):
        return row_text

    headers = _table_headers(row, len(cell_texts))
    selected_header = headers[selected_index] if selected_index is not None and selected_index < len(headers) else ""
    if headers:
        pairs = []
        heading = _nearby_heading(row)
        if heading:
            pairs.append(f"Family: {heading}")
        for group in _table_group_context(row):
            pairs.append(f"Group: {group}")
        if selected_header:
            pairs.append(f"Selected option: {selected_header}")
        for index, (cell_element, cell) in enumerate(zip(cell_elements, cell_texts)):
            header = headers[index] if index < len(headers) else ""
            if _skip_order_cell(cell_element, index, selected_index):
                continue
            cell = PART_RE.sub(" ", cell)
            cell = clean_text(cell)
            if not cell:
                continue
            pairs.append(f"{header}: {cell}" if header else cell)
        context = clean_text("; ".join(pairs))
        if context:
            return context
    return row_text


def _table_group_context(row) -> list[str]:
    table = row.find_parent("table")
    if table is None:
        return []

    groups_by_span: dict[int, str] = {}
    current = row
    checked = 0
    while checked < 5000:
        current = current.find_previous("tr")
        if current is None or current.find_parent("table") is not table:
            break
        checked += 1
        text = clean_text(current.get_text(" ", strip=True))
        if not text:
            continue
        if _is_header_row(current):
            break
        if PART_RE.search(text):
            continue
        if not _is_table_group_row(current, text):
            continue
        span = _table_group_span(current)
        if span not in groups_by_span:
            groups_by_span[span] = text[:180]
        if len(groups_by_span) >= 5:
            break

    return [
        groups_by_span[span]
        for span in sorted(groups_by_span, reverse=True)
    ]


def _is_header_row(row) -> bool:
    if row.find_parent("thead") is not None:
        return True
    classes = " ".join(row.get("class", []))
    return "headerRow" in classes


def _is_table_group_row(row, text: str) -> bool:
    if len(text) < 2 or len(text) > 180:
        return False
    classes = " ".join(row.get("class", []))
    if "stackPivotRow" in classes:
        return True
    cells = row.find_all(["td", "th"], recursive=False)
    nonempty_cells = [
        clean_text(cell.get_text(" ", strip=True))
        for cell in cells
        if clean_text(cell.get_text(" ", strip=True))
    ]
    return len(nonempty_cells) == 1 and len(cells) <= 3


def _table_group_span(row) -> int:
    spans = []
    for cell in row.find_all(["td", "th"], recursive=False):
        text = clean_text(cell.get_text(" ", strip=True))
        if not text:
            continue
        try:
            spans.append(int(cell.get("colspan") or 1))
        except ValueError:
            spans.append(1)
    return max(spans) if spans else 1


def _skip_order_cell(cell, index: int, selected_index: int | None) -> bool:
    classes = " ".join(cell.get("class", []))
    is_order_cell = any(
        marker in classes
        for marker in ("partNumberCell", "priceCell", "nullCell")
    )
    if not is_order_cell:
        return False
    return selected_index is None or index != selected_index


def _nearby_heading(element) -> str:
    table = element.find_parent("table")
    start = table or element
    for heading in start.find_all_previous(["h1", "h2", "h3", "h4"]):
        text = clean_text(heading.get_text(" ", strip=True))
        if _is_good_product_name(text):
            return text[:180]
    return ""


def _cell_index(element, row, part_number: str) -> int | None:
    part = part_number.upper()
    cells = row.find_all(["td", "th"], recursive=False)
    for index, cell in enumerate(cells):
        if cell is element or element in cell.descendants:
            cell_text = clean_text(cell.get_text(" ", strip=True))
            cell_attrs = " ".join(
                str(cell.get(attr, "")) for attr in ("href", "src", "srcset", "data-src", "data-srcset")
            )
            if part in f"{cell_text} {cell_attrs}".upper():
                return index
    for index, cell in enumerate(cells):
        blob = clean_text(cell.get_text(" ", strip=True))
        if part in blob.upper():
            return index
    return None


def _table_headers(row, data_cell_count: int) -> list[str]:
    table = row.find_parent("table")
    if table is None:
        return []
    header_rows = []
    thead = table.find("thead")
    if thead is not None:
        header_rows.extend(thead.find_all("tr"))
    if not header_rows:
        header_rows.extend(table.find_all("tr")[:3])
    expanded_rows: list[list[str]] = []
    for header_row in header_rows:
        cells = header_row.find_all(["th", "td"], recursive=False)
        if not cells:
            continue
        expanded: list[str] = []
        for cell in cells:
            text = clean_text(cell.get_text(" ", strip=True))
            try:
                colspan = int(cell.get("colspan") or 1)
            except ValueError:
                colspan = 1
            expanded.extend([text] * max(colspan, 1))
        if any(expanded):
            expanded_rows.append(expanded)
    if not expanded_rows:
        return []

    width = max(len(row) for row in expanded_rows)
    headers = []
    for index in range(width):
        parts = []
        for expanded in expanded_rows:
            text = expanded[index] if index < len(expanded) else ""
            if text and text not in parts:
                parts.append(text)
        headers.append(" ".join(parts))

    while headers and not headers[0] and len(headers) > data_cell_count:
        headers.pop(0)
    if len(headers) > data_cell_count:
        headers = headers[:data_cell_count]
    return headers


def _selected_part_header(headers: list[str], selected_index: int | None) -> str:
    if selected_index is None or selected_index >= len(headers):
        return ""
    return headers[selected_index]


def _best_part_context(soup: BeautifulSoup, part_number: str) -> str:
    part = part_number.upper()
    candidates: list[tuple[int, int, str]] = []
    for element in soup.find_all(True):
        text = clean_text(element.get_text(" ", strip=True))
        if part not in text.upper():
            continue
        if len(part) + 4 <= len(text) <= 900:
            candidates.append((abs(len(text) - 180), len(text), text))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _clean_product_name(value: str, part_number: str) -> str:
    text = clean_text(value)
    text = re.sub(r"\b" + re.escape(part_number) + r"\b", "", text, flags=re.IGNORECASE)
    text = PART_RE.sub(" ", text)
    text = clean_text(text)
    if not _is_good_product_name(text):
        return ""
    return text[:180]


def _clean_product_context(value: str, part_number: str) -> str:
    text = clean_text(value)
    if not text:
        return ""
    text = PART_RE.sub(" ", text)
    text = clean_text(text)
    if len(text) < 3:
        return ""
    return text[:700]


def _is_good_product_name(value: str) -> bool:
    text = clean_text(value)
    if len(text) < 3 or len(text) > 240:
        return False
    if text.lower() in {"teststring", "compare", "add to order", "product detail"}:
        return False
    if PART_RE.fullmatch(text):
        return False
    return True


def _title_from_soup(soup: BeautifulSoup) -> str:
    title = soup.find("title")
    if title is None:
        return ""
    return clean_text(title.get_text()).replace(" | McMaster-Carr", "")


def _has_spec_table(soup: BeautifulSoup) -> bool:
    for element in soup.find_all(class_=True):
        classes = " ".join(element.get("class", []))
        if "spec-table" in classes or "product-detail-spec" in classes:
            return True
    return False
