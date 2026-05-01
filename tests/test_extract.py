from mcmaster_navigator_mcp.extract import (
    extract_part_numbers,
    normalize_target,
    search_url,
    snapshot_from_html,
)
from mcmaster_navigator_mcp.models import FindPartsResult


def test_normalize_target_handles_part_paths_and_queries():
    assert normalize_target("47865K23") == "https://www.mcmaster.com/47865K23"
    assert normalize_target("/products/screws/") == "https://www.mcmaster.com/products/screws/"
    assert normalize_target("stainless steel bolt") == "https://www.mcmaster.com/stainless+steel+bolt"
    assert search_url("ball valve brass") == "https://www.mcmaster.com/ball+valve+brass"


def test_extract_part_numbers_dedupes_and_uppercases():
    assert extract_part_numbers("Use 47865k23 or 47865K23 with 91251A542.") == [
        "47865K23",
        "91251A542",
    ]


def test_snapshot_extracts_products_and_prioritizes_content_links():
    html = """
    <html>
      <head><title>Screws | McMaster-Carr</title></head>
      <body>
        <a href="/products/abrading-and-polishing/">Abrading &amp; Polishing</a>
        <main>
          <a href="/products/socket-head-screws/">Socket Head Screws</a>
          <a href="/91251A542">Alloy Steel Socket Head Screw</a>
          <img srcset="https://images1.mcmaster.com/91251a542p1.png" alt="Socket Head Screws">
        </main>
      </body>
    </html>
    """
    snapshot = snapshot_from_html(html, "https://www.mcmaster.com/products/screws/")

    assert snapshot.page_type == "category"
    assert snapshot.products[0].part_number == "91251A542"
    assert snapshot.products[0].name == "Alloy Steel Socket Head Screw"
    assert snapshot.links[0].text == "Socket Head Screws"
    assert snapshot.links[0].kind == "category"
    assert snapshot.links[1].text == "Alloy Steel Socket Head Screw"
    assert snapshot.links[1].kind == "product"


def test_find_parts_result_uses_compact_page_summaries():
    html = """
    <html>
      <body>
        <a href="/products/socket-head-screws/">Socket Head Screws</a>
        <a href="/91251A542">Alloy Steel Socket Head Screw</a>
      </body>
    </html>
    """
    snapshot = snapshot_from_html(html, "https://www.mcmaster.com/products/screws/")
    result = FindPartsResult("socket screw", snapshot.products, [snapshot]).to_dict()

    assert result["pages_visited"][0]["product_count"] == 1
    assert result["pages_visited"][0]["link_count"] == 2
    assert "links" not in result["pages_visited"][0]
    assert "text_preview" not in result["pages_visited"][0]
