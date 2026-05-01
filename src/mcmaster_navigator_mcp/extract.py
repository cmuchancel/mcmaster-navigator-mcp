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

    def add(part_number: str, name: str, source: str, confidence: float) -> None:
        part = part_number.upper()
        label = _clean_product_name(name, part)
        existing = hits.get(part)
        if existing is None:
            hits[part] = ProductHit(
                part_number=part,
                name=label,
                url=product_url(part),
                sources=[source],
                confidence=confidence,
            )
            return
        if source not in existing.sources:
            existing.sources.append(source)
        if label and (not existing.name or existing.name == part or len(label) > len(existing.name)):
            existing.name = label
        existing.confidence = max(existing.confidence, confidence)

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        for part in _parts_from_url(href):
            add(part, _anchor_context_name(anchor), "link", 0.95)

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
            add(part, label, "image", 0.8)

    for part in extract_part_numbers(str(soup)):
        if part not in hits:
            add(part, "", "html", 0.45)

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


def _clean_product_name(value: str, part_number: str) -> str:
    text = clean_text(value)
    text = re.sub(r"\b" + re.escape(part_number) + r"\b", "", text, flags=re.IGNORECASE)
    text = clean_text(text)
    if not _is_good_product_name(text):
        return ""
    return text[:180]


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
