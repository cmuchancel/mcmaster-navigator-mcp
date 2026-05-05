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
    schemas = extract_page_schema(soup, url, links=links)
    text_preview = clean_text(soup.get_text(" ", strip=True))[:1200]
    part_numbers = sorted({product.part_number for product in products} | set(extract_part_numbers(html)))
    return PageSnapshot(
        url=url,
        title=page_title,
        page_type=detect_page_type(url, page_title, soup),
        products=products,
        links=links,
        schemas=schemas,
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

    def add(
        part_number: str,
        name: str,
        source: str,
        confidence: float,
        context: str = "",
        *,
        attributes: dict[str, str] | None = None,
        family: str = "",
        groups: list[str] | None = None,
        selected_option: str = "",
    ) -> None:
        part = part_number.upper()
        label = _clean_product_name(name, part)
        product_context = _clean_product_context(context, part)
        row_attributes = _clean_attributes(attributes or {}, part)
        row_groups = [clean_text(group) for group in (groups or []) if clean_text(group)]
        existing = hits.get(part)
        if existing is None:
            hits[part] = ProductHit(
                part_number=part,
                name=label,
                url=product_url(part),
                context=product_context,
                attributes=row_attributes,
                family=clean_text(family),
                groups=row_groups,
                selected_option=clean_text(selected_option),
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
        for key, value in row_attributes.items():
            if key not in existing.attributes or len(value) > len(existing.attributes[key]):
                existing.attributes[key] = value
        if family and (not existing.family or len(family) > len(existing.family)):
            existing.family = clean_text(family)
        for group in row_groups:
            if group not in existing.groups:
                existing.groups.append(group)
        if selected_option and not existing.selected_option:
            existing.selected_option = clean_text(selected_option)
        existing.confidence = max(existing.confidence, confidence)

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        for part in _parts_from_url(href):
            row_data = _part_data_from_element(anchor, part)
            add(
                part,
                _anchor_context_name(anchor),
                "link",
                0.95,
                row_data["context"],
                attributes=row_data["attributes"],
                family=row_data["family"],
                groups=row_data["groups"],
                selected_option=row_data["selected_option"],
            )

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
            row_data = _part_data_from_element(image, part)
            add(
                part,
                label,
                "image",
                0.8,
                row_data["context"],
                attributes=row_data["attributes"],
                family=row_data["family"],
                groups=row_data["groups"],
                selected_option=row_data["selected_option"],
            )

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


def extract_page_schema(
    soup: BeautifulSoup,
    current_url: str,
    *,
    links: list[PageLink] | None = None,
    max_tables: int = 12,
    max_rows_per_table: int = 160,
) -> list[dict[str, object]]:
    """Extract live table/filter schemas without assuming a part-family ontology."""
    page_title = _title_from_soup(soup)
    page_links = links if links is not None else extract_links(soup, current_url, max_links=200)
    filters = [
        {
            "text": link.text,
            "url": link.url,
        }
        for link in page_links
        if link.kind == "filter"
    ]
    tables = []
    for table_index, table in enumerate(soup.find_all("table")):
        if len(tables) >= max_tables:
            break
        table_rows = _extract_schema_rows(table, table_index, max_rows_per_table)
        if not table_rows:
            continue
        columns: list[str] = []
        families: list[str] = []
        part_numbers: list[str] = []
        for row in table_rows:
            family = str(row.get("family") or "")
            if family and family not in families:
                families.append(family)
            for part_number in row.get("part_numbers", []):
                if isinstance(part_number, str) and part_number not in part_numbers:
                    part_numbers.append(part_number)
            attributes = row.get("attributes", {})
            if isinstance(attributes, dict):
                for column in attributes:
                    if column and column not in columns:
                        columns.append(column)
        table_title = families[0] if families else _nearby_heading(table) or page_title
        tables.append(
            {
                "index": table_index,
                "title": table_title,
                "columns": columns,
                "row_count": len(table_rows),
                "part_numbers": part_numbers,
                "rows": table_rows,
            }
        )
    tables.sort(
        key=lambda table: (
            0 if table.get("columns") else 1,
            -len(table.get("columns", [])),
            -int(table.get("row_count", 0)),
            int(table.get("index", 0)),
        )
    )
    if not filters and not tables:
        return []
    return [
        {
            "family_title": page_title,
            "filters": filters[:100],
            "tables": tables,
        }
    ]


def _extract_schema_rows(table, table_index: int, max_rows: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, tuple[tuple[str, str], ...], tuple[str, ...]]] = set()

    def add_row(row_data: dict[str, object]) -> bool:
        attributes = row_data["attributes"] if isinstance(row_data["attributes"], dict) else {}
        groups = row_data["groups"] if isinstance(row_data["groups"], list) else []
        part_number = str(row_data["part_number"])
        key = (
            part_number,
            tuple(sorted((str(k), str(v)) for k, v in attributes.items())),
            tuple(str(group) for group in groups),
        )
        if key in seen:
            return False
        seen.add(key)
        rows.append(row_data)
        return len(rows) >= max_rows

    for row_index, row in enumerate(table.find_all("tr")):
        part_numbers = _primary_row_part_numbers(row) or extract_part_numbers(str(row))
        if not part_numbers:
            continue
        base_data = None
        for part_number in part_numbers:
            data = _table_row_data_from_row(row, part_number)
            if base_data is None:
                base_data = data
            row_data = _schema_row_from_data(
                table_index=table_index,
                row_index=row_index,
                part_number=part_number,
                data=data,
            )
            if add_row(row_data):
                return rows
        if base_data is not None:
            for row_data in _linked_option_rows(
                row,
                table_index=table_index,
                row_index=row_index,
                base_part_numbers=part_numbers,
                base_data=base_data,
            ):
                if add_row(row_data):
                    return rows
    return rows


def _schema_row_from_data(
    *,
    table_index: int,
    row_index: int,
    part_number: str,
    data: dict[str, object],
) -> dict[str, object]:
    attributes = data["attributes"] if isinstance(data["attributes"], dict) else {}
    groups = data["groups"] if isinstance(data["groups"], list) else []
    return {
        "table_index": table_index,
        "row_index": row_index,
        "part_number": part_number,
        "part_numbers": [part_number],
        "family": data["family"],
        "groups": groups,
        "selected_option": data["selected_option"],
        "attributes": attributes,
        "evidence": data["context"],
    }


def _primary_row_part_numbers(row) -> list[str]:
    parts: list[str] = []
    cells = row.find_all(["td", "th"], recursive=False)
    for cell in cells:
        classes = " ".join(cell.get("class", [])).lower()
        if "partnumbercell" not in classes and "part-number" not in classes:
            continue
        for part_number in extract_part_numbers(cell.get_text(" ", strip=True)):
            if part_number not in parts:
                parts.append(part_number)
    return parts


def _linked_option_rows(
    row,
    *,
    table_index: int,
    row_index: int,
    base_part_numbers: list[str],
    base_data: dict[str, object],
) -> list[dict[str, object]]:
    base_parts = {part.upper() for part in base_part_numbers}
    base_attributes = base_data["attributes"] if isinstance(base_data["attributes"], dict) else {}
    base_groups = base_data["groups"] if isinstance(base_data["groups"], list) else []
    cell_elements = row.find_all(["td", "th"], recursive=False)
    cell_texts = [clean_text(cell.get_text(" ", strip=True)) for cell in cell_elements]
    headers = _table_headers(row, len(cell_texts))
    option_rows: list[dict[str, object]] = []
    for index, cell in enumerate(cell_elements):
        if not _looks_like_option_cell(cell):
            continue
        header = headers[index] if index < len(headers) else ""
        option_key = header or "Selected Option"
        for anchor in cell.find_all("a", href=True):
            option_text = clean_text(anchor.get_text(" ", strip=True))
            if not option_text or PART_RE.fullmatch(option_text):
                continue
            linked_parts = extract_part_numbers(anchor.get("href", ""))
            variant_part = next((part for part in reversed(linked_parts) if part not in base_parts), "")
            if not variant_part:
                continue
            attributes = {str(key): str(value) for key, value in base_attributes.items()}
            attributes[option_key] = option_text
            context = clean_text(f"{base_data['context']}; {option_key}: {option_text}")
            option_rows.append(
                {
                    "table_index": table_index,
                    "row_index": row_index,
                    "part_number": variant_part,
                    "part_numbers": [variant_part],
                    "family": base_data["family"],
                    "groups": base_groups,
                    "selected_option": option_text,
                    "attributes": attributes,
                    "evidence": context,
                    "option_variant": True,
                    "option_field": option_key,
                    "base_part_numbers": list(base_part_numbers),
                }
            )
    return option_rows


def _looks_like_option_cell(cell) -> bool:
    classes = " ".join(cell.get("class", [])).lower()
    if "easytoorder" in classes:
        return True
    anchors = cell.find_all("a", href=True)
    return any(extract_part_numbers(anchor.get("href", "")) and clean_text(anchor.get_text(" ", strip=True)) for anchor in anchors)


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


def _part_data_from_element(element, part_number: str) -> dict[str, object]:
    table_data = _table_row_data(element, part_number)
    if table_data["context"]:
        return table_data
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
        return _empty_part_data()
    candidates.sort(key=lambda item: (item[0], item[1]))
    return {
        **_empty_part_data(),
        "context": candidates[0][2],
    }


def _table_row_context(element, part_number: str) -> str:
    return str(_table_row_data(element, part_number)["context"])


def _table_row_data(element, part_number: str) -> dict[str, object]:
    row = element.find_parent("tr")
    if row is None:
        return _empty_part_data()
    return _table_row_data_from_row(row, part_number, element=element)


def _table_row_data_from_row(row, part_number: str, *, element=None) -> dict[str, object]:
    row_text = clean_text(row.get_text(" ", strip=True))
    if part_number.upper() not in row_text.upper() and part_number.upper() not in str(row).upper():
        return _empty_part_data()

    selected_index = _cell_index(element, row, part_number)
    cell_elements = row.find_all(["td", "th"], recursive=False)
    cell_texts = [clean_text(cell.get_text(" ", strip=True)) for cell in cell_elements]
    if not any(cell_texts):
        return {
            **_empty_part_data(),
            "context": row_text,
        }

    headers = _table_headers(row, len(cell_texts))
    selected_header = headers[selected_index] if selected_index is not None and selected_index < len(headers) else ""
    family = ""
    groups: list[str] = []
    attributes: dict[str, str] = {}
    if headers:
        pairs = []
        family = _nearby_heading(row)
        if family:
            pairs.append(f"Family: {family}")
        groups = _merge_group_contexts(
            _section_group_context(row),
            _table_group_context(row),
        )
        for group in groups:
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
            if header:
                attributes[header] = cell
            pairs.append(f"{header}: {cell}" if header else cell)
        context = clean_text("; ".join(pairs))
        if context:
            return {
                "context": context,
                "attributes": attributes,
                "family": family,
                "groups": groups,
                "selected_option": selected_header,
            }
    return {
        **_empty_part_data(),
        "context": row_text,
    }


def _empty_part_data() -> dict[str, object]:
    return {
        "context": "",
        "attributes": {},
        "family": "",
        "groups": [],
        "selected_option": "",
    }


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


def _section_group_context(row) -> list[str]:
    table = row.find_parent("table")
    if table is None:
        return []

    groups: list[str] = []
    for element in table.find_all_previous(True, limit=200):
        if element.find_parent("table") is table:
            continue
        text = clean_text(element.get_text(" ", strip=True))
        if not _is_section_group_header(element, text):
            continue
        if text not in groups:
            groups.append(text[:180])
        if len(groups) >= 4:
            break
    return groups


def _is_section_group_header(element, text: str) -> bool:
    if len(text) < 2 or len(text) > 180:
        return False
    if PART_RE.search(text):
        return False
    classes = " ".join(element.get("class", [])).lower()
    return any(
        marker in classes
        for marker in (
            "subtableheader",
            "presentationheader",
            "sectionheader",
            "categoryheader",
            "tableheader",
        )
    )


def _merge_group_contexts(*group_lists: list[str]) -> list[str]:
    groups: list[str] = []
    for group_list in group_lists:
        for group in group_list:
            group = clean_text(group)
            if group and group not in groups:
                groups.append(group)
    return groups


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
        for marker in ("partNumberCell", "priceCell")
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


def _clean_attributes(attributes: dict[str, str], part_number: str) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in attributes.items():
        clean_key = clean_text(str(key)).strip(" :")
        clean_value = clean_text(str(value))
        clean_value = PART_RE.sub(" ", clean_value)
        clean_value = clean_text(clean_value)
        if not clean_key or not clean_value:
            continue
        if part_number.upper() == clean_value.upper():
            continue
        cleaned[clean_key] = clean_value[:240]
    return cleaned


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
