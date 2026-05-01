from __future__ import annotations

import os
import platform
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .extract import BASE_URL, PART_RE, normalize_target, search_url, snapshot_from_html
from .models import FindPartsResult, PageSnapshot, ProductHit


@dataclass
class NavigatorConfig:
    profile_dir: Path
    ephemeral_profile: bool = True
    page_load_timeout_seconds: int = 45
    settle_seconds: float = 3.0
    max_products: int = 80
    max_links: int = 100
    auto_drill_depth: int = 2

    @classmethod
    def from_env(cls) -> "NavigatorConfig":
        profile = os.environ.get("MCMASTER_NAV_PROFILE_DIR")
        if profile:
            profile_dir = Path(profile).expanduser()
            ephemeral = False
        else:
            profile_dir = Path(tempfile.mkdtemp(prefix="mcmaster-navigator-"))
            ephemeral = True
        return cls(
            profile_dir=profile_dir,
            ephemeral_profile=ephemeral,
            page_load_timeout_seconds=int(os.environ.get("MCMASTER_NAV_PAGE_TIMEOUT", "45")),
            settle_seconds=float(os.environ.get("MCMASTER_NAV_SETTLE_SECONDS", "3")),
            max_products=int(os.environ.get("MCMASTER_NAV_MAX_PRODUCTS", "80")),
            max_links=int(os.environ.get("MCMASTER_NAV_MAX_LINKS", "100")),
            auto_drill_depth=int(os.environ.get("MCMASTER_NAV_AUTO_DRILL_DEPTH", "2")),
        )


class McMasterNavigator:
    """Headless McMaster-Carr navigator backed by SeleniumBase UC mode."""

    def __init__(self, config: NavigatorConfig | None = None):
        self.config = config or NavigatorConfig.from_env()
        self._sb_context = None
        self._sb = None
        self._trail: list[str] = []

    def start(self) -> None:
        if self._sb is not None:
            return
        self.config.profile_dir.mkdir(parents=True, exist_ok=True)
        self._clear_profile_locks()
        from seleniumbase import SB

        self._sb_context = SB(
            uc=True,
            headless2=True,
            test=True,
            user_data_dir=str(self.config.profile_dir),
        )
        self._sb = self._sb_context.__enter__()
        try:
            self._sb.driver.set_page_load_timeout(self.config.page_load_timeout_seconds)
        except Exception:
            pass

    def close(self) -> None:
        if self._sb_context is not None:
            try:
                self._sb_context.__exit__(None, None, None)
            except Exception:
                pass
        self._sb_context = None
        self._sb = None
        if self.config.ephemeral_profile:
            shutil.rmtree(self.config.profile_dir, ignore_errors=True)

    def current_page(self) -> PageSnapshot:
        self.start()
        if not self._current_url():
            return self.open(BASE_URL)
        return self._snapshot()

    def open(self, target: str) -> PageSnapshot:
        self.start()
        url = normalize_target(target, self._current_url() or BASE_URL)
        self._open_url(url)
        return self._snapshot()

    def search(self, query: str, *, max_depth: int | None = None) -> PageSnapshot:
        self.start()
        depth_limit = self.config.auto_drill_depth if max_depth is None else max_depth
        self._open_url(search_url(query))
        snapshot = self._snapshot()

        depth = 0
        while depth < depth_limit and len(snapshot.products) < min(20, self.config.max_products):
            previous_count = len(snapshot.products)
            if not self._click_best_category_candidate():
                break
            next_snapshot = self._snapshot()
            if len(next_snapshot.products) <= previous_count:
                break
            snapshot = next_snapshot
            depth += 1
        snapshot.trail = list(self._trail)
        snapshot.diagnostics["auto_drill_depth_used"] = depth
        return snapshot

    def find_parts(
        self,
        query: str,
        *,
        max_results: int = 50,
        max_pages: int = 4,
        auto_drill_depth: int | None = None,
    ) -> FindPartsResult:
        pages: list[PageSnapshot] = []
        first = self.search(query, max_depth=auto_drill_depth)
        pages.append(first)
        products = _merge_products([], first.products)

        if len(products) < max_results and max_pages > 1:
            for link in _rank_links_for_query(first, query):
                if len(pages) >= max_pages or len(products) >= max_results:
                    break
                try:
                    page = self.open(link.url)
                except Exception:
                    continue
                pages.append(page)
                products = _merge_products(products, page.products)

        return FindPartsResult(
            query=query,
            products=products[:max_results],
            pages=pages,
            diagnostics={
                "max_results": max_results,
                "max_pages": max_pages,
                "unique_part_numbers": len({product.part_number for product in products}),
            },
        )

    def follow_link(
        self,
        *,
        index: int | None = None,
        text: str | None = None,
        url: str | None = None,
    ) -> PageSnapshot:
        if url:
            return self.open(url)
        current = self.current_page()
        selected = None
        if index is not None:
            selected = next((link for link in current.links if link.index == index), None)
            if selected is None:
                raise ValueError(f"No link found with index {index}")
        elif text:
            needle = text.lower()
            selected = next((link for link in current.links if needle in link.text.lower()), None)
            if selected is None:
                raise ValueError(f"No link found containing text: {text}")
        else:
            raise ValueError("Provide index, text, or url")
        return self.open(selected.url)

    def back(self) -> PageSnapshot:
        self.start()
        self._sb.driver.back()
        time.sleep(min(self.config.settle_seconds, 2.0))
        return self._snapshot()

    def doctor(self) -> dict[str, Any]:
        return {
            "ok": True,
            "platform": platform.platform(),
            "python": platform.python_version(),
            "chrome_on_path": shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome"),
            "mac_chrome_app": Path("/Applications/Google Chrome.app").exists(),
            "profile_dir": str(self.config.profile_dir),
            "ephemeral_profile": self.config.ephemeral_profile,
            "headless": True,
            "backend": "seleniumbase-uc-headless2",
        }

    def _clear_profile_locks(self) -> None:
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            path = self.config.profile_dir / name
            try:
                if path.is_dir() or path.is_symlink():
                    path.unlink()
                elif path.exists():
                    path.unlink()
            except Exception:
                pass

    def _open_url(self, url: str) -> None:
        assert self._sb is not None
        self._sb.open(url)
        self._wait_for_render()
        current_url = self._current_url()
        if current_url:
            self._trail.append(current_url)
            self._trail = self._trail[-12:]

    def _wait_for_render(self) -> None:
        time.sleep(self.config.settle_seconds)

    def _snapshot(self) -> PageSnapshot:
        assert self._sb is not None
        html = self._sb.get_page_source()
        url = self._current_url() or BASE_URL
        try:
            title = self._sb.get_title()
        except Exception:
            title = ""
        return snapshot_from_html(
            html,
            url,
            title,
            max_products=self.config.max_products,
            max_links=self.config.max_links,
            trail=list(self._trail),
        )

    def _current_url(self) -> str:
        if self._sb is None:
            return ""
        try:
            return self._sb.get_current_url()
        except Exception:
            return ""

    def _click_best_category_candidate(self) -> bool:
        assert self._sb is not None
        selectors = [
            '[class*="outerContainer"]',
            '[class*="GridCell"] a',
            '[class*="flexTable"] a',
            'a[href*="/products/"]',
        ]
        for selector in selectors:
            try:
                elements = self._sb.find_elements(selector)
            except Exception:
                continue
            for element in elements[:12]:
                if self._skip_click_candidate(element):
                    continue
                try:
                    self._sb.execute_script("arguments[0].click();", element)
                except Exception:
                    try:
                        element.click()
                    except Exception:
                        continue
                self._wait_for_render()
                current_url = self._current_url()
                if current_url:
                    self._trail.append(current_url)
                    self._trail = self._trail[-12:]
                return True
        return False

    def _skip_click_candidate(self, element) -> bool:
        try:
            href = element.get_attribute("href") or ""
            text = (element.text or "").strip().lower()
        except Exception:
            return True
        if not href:
            return not text or text in {"home", "order", "order history", "log in"}
        if href and PART_RE.search(href):
            return True
        path = urlparse(href).path.lower() if href else ""
        if path in {"", "/", "/orders", "/order-history"}:
            return True
        if text in {"home", "order", "order history", "log in"}:
            return True
        return False


def _merge_products(existing: list[ProductHit], incoming: list[ProductHit]) -> list[ProductHit]:
    by_part = {product.part_number: product for product in existing}
    for product in incoming:
        current = by_part.get(product.part_number)
        if current is None:
            by_part[product.part_number] = product
            continue
        for source in product.sources:
            if source not in current.sources:
                current.sources.append(source)
        if product.name and (not current.name or len(product.name) > len(current.name)):
            current.name = product.name
        current.confidence = max(current.confidence, product.confidence)
    return sorted(by_part.values(), key=lambda item: (-item.confidence, item.part_number))


def _rank_links_for_query(snapshot: PageSnapshot, query: str):
    terms = [term for term in re_split_query(query) if len(term) > 2]

    def score(link) -> tuple[int, str]:
        text = link.text.lower()
        term_score = sum(1 for term in terms if term in text)
        kind_score = {"product": 0, "category": 1, "catalog_category": 3}.get(link.kind, 5)
        return (-term_score + kind_score, text)

    return sorted(
        [link for link in snapshot.links if link.kind in {"category", "catalog_category", "product"}],
        key=score,
    )


def re_split_query(query: str) -> list[str]:
    return [part.lower() for part in re_split_nonword(query) if part]


def re_split_nonword(value: str) -> list[str]:
    import re

    return re.split(r"[^a-zA-Z0-9]+", value)
