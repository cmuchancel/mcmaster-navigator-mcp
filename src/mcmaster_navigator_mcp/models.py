from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ProductHit:
    part_number: str
    name: str = ""
    url: str = ""
    sources: list[str] = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PageLink:
    index: int
    text: str
    url: str
    kind: str = "link"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PageSnapshot:
    url: str
    title: str
    page_type: str
    products: list[ProductHit] = field(default_factory=list)
    links: list[PageLink] = field(default_factory=list)
    part_numbers: list[str] = field(default_factory=list)
    text_preview: str = ""
    trail: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "page_type": self.page_type,
            "product_count": len(self.products),
            "products": [product.to_dict() for product in self.products],
            "part_numbers": self.part_numbers,
            "link_count": len(self.links),
            "links": [link.to_dict() for link in self.links],
            "text_preview": self.text_preview,
            "trail": self.trail,
            "diagnostics": self.diagnostics,
        }

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "page_type": self.page_type,
            "product_count": len(self.products),
            "part_numbers": self.part_numbers,
            "link_count": len(self.links),
            "trail": self.trail,
            "diagnostics": self.diagnostics,
        }


@dataclass
class FindPartsResult:
    query: str
    products: list[ProductHit]
    pages: list[PageSnapshot]
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "count": len(self.products),
            "products": [product.to_dict() for product in self.products],
            "pages_visited": [page.to_summary_dict() for page in self.pages],
            "diagnostics": self.diagnostics,
        }
