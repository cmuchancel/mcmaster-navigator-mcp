from mcmaster_navigator_mcp.extract import (
    extract_part_numbers,
    normalize_target,
    search_url,
    snapshot_from_html,
)
from mcmaster_navigator_mcp.models import FindPartsResult, ProductHit
from mcmaster_navigator_mcp.rank import derive_search_queries, derive_search_query, rank_products


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
    assert "Alloy Steel Socket Head Screw" in snapshot.products[0].context
    assert snapshot.links[0].text == "Socket Head Screws"
    assert snapshot.links[0].kind == "category"
    assert snapshot.links[1].text == "Alloy Steel Socket Head Screw"
    assert snapshot.links[1].kind == "product"


def test_mcmaster_family_links_rank_before_filter_links():
    html = """
    <html>
      <body>
        <a href="/compression+springs/length~0-001-to-6-1/">0.001" to 6"</a>
        <a href="/compression+springs/compression-springs-1~~/">Compression Springs</a>
      </body>
    </html>
    """
    snapshot = snapshot_from_html(html, "https://www.mcmaster.com/compression+springs/")

    assert snapshot.links[0].text == "Compression Springs"
    assert snapshot.links[0].kind == "category"
    assert snapshot.links[1].kind == "filter"


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


def test_exact_part_ranking_prefers_matching_row_context():
    html = """
    <html>
      <head><title>Stainless Steel Socket Head Screws | McMaster-Carr</title></head>
      <body>
        <table>
          <tr><td>M14 x 2 mm</td><td>25 mm</td><td>Fully Threaded</td><td><a href="/90696A101">90696A101</a></td></tr>
          <tr><td>M16 x 2 mm</td><td>40 mm</td><td>Partially Threaded</td><td><a href="/90696A155">90696A155</a></td></tr>
        </table>
      </body>
    </html>
    """
    snapshot = snapshot_from_html(html, "https://www.mcmaster.com/stainless-steel-socket-head-screws/")
    ranked = rank_products(
        "stainless steel socket head screw M14 x 2 mm 25 mm fully threaded",
        snapshot.products,
        page_title=snapshot.title,
    )

    assert ranked[0].product.part_number == "90696A101"


def test_derive_search_query_uses_family_phrase_not_specs():
    query = derive_search_query(
        'Surface-Mount Hinges with Holes. 2 3/8" long 316 stainless steel polished 180 degree opening.'
    )

    assert query == "surface-mount hinges holes"


def test_derive_search_query_prefers_first_family_over_category():
    query = derive_search_query(
        'door hinges. 2 3/8" leaf length 1 3/16" leaf width 316 stainless steel polished. Building & Grounds'
    )

    assert query == "door hinges"


def test_derive_search_query_prefers_family_over_long_spec_segment():
    query = derive_search_query(
        'toggle switch, 1 Off Maintained SPST-NO On-Off 6 amp 125V AC quick-disconnect terminal'
    )

    assert query == "toggle switch"


def test_derive_search_queries_adds_catalog_friendly_l_key_variants():
    queries = derive_search_queries(
        'hex key wrench. Gold-Painted Alloy Steel Hex L-Key, 1.5 mm Drive Size, 3-1/16" Overall Length.',
        limit=8,
    )

    assert "gold painted alloy hex l key" in queries
    assert "hex l-key" in queries


def test_derive_search_queries_adds_catalog_friendly_spring_plural():
    queries = derive_search_queries(
        'compression spring. Music-Wire Steel Compression Springs, 0.813" Long, 0.24" OD, 0.196" ID.',
        limit=8,
    )

    assert "compression springs" in queries
    assert "music wire springs" in queries


def test_field_aware_ranking_keeps_numbers_in_the_right_column():
    html = """
    <html>
      <head><title>Surface-Mount Hinges with Holes | McMaster-Carr</title></head>
      <body>
        <table>
          <thead>
            <tr><th>Door Leaf Ht.</th><th>Door Leaf Wd.</th><th>Overall Wd.</th><th></th></tr>
          </thead>
          <tr><td>6"</td><td>3/4"</td><td>1 1/2"</td><td><a href="/1586A12">1586A12</a></td></tr>
          <tr><td>6"</td><td>1 1/2"</td><td>3"</td><td><a href="/1586A21">1586A21</a></td></tr>
        </table>
      </body>
    </html>
    """
    snapshot = snapshot_from_html(html, "https://www.mcmaster.com/door-hinges/")
    ranked = rank_products(
        "Surface-mount hinge with holes, removable pin, 6 inch x 1-1/2 inch door leaf",
        snapshot.products,
        page_title=snapshot.title,
    )

    assert ranked[0].product.part_number == "1586A21"


def test_table_row_context_includes_stack_pivot_groups_for_exact_specs():
    html = """
    <html>
      <head><title>Stainless Steel Socket Head Screws | McMaster-Carr</title></head>
      <body>
        <h2>Stainless Steel Socket Head Screws</h2>
        <table>
          <thead>
            <tr><th></th><th>Lg.</th><th>Pkg. Qty.</th><th></th><th>Pkg.</th></tr>
          </thead>
          <tr class="_stackPivotRow_q8hpp_63"><td colspan="5">18-8 Stainless Steel</td></tr>
          <tr class="_stackPivotRow_q8hpp_63"><td colspan="4">M10 x 1.5 mm</td></tr>
          <tr><td>2 mm</td><td>5</td><td class="_partNumberCell_krvpj_1"><a href="/91292A333">91292A333</a></td><td class="_priceCell_14fib_77">8.00</td></tr>
          <tr class="_stackPivotRow_q8hpp_63"><td colspan="4">M14 x 2 mm</td></tr>
          <tr><td>25 mm</td><td>5</td><td class="_partNumberCell_krvpj_1"><a href="/90696A101">90696A101</a></td><td class="_priceCell_14fib_77">8.76</td></tr>
        </table>
      </body>
    </html>
    """
    snapshot = snapshot_from_html(html, "https://www.mcmaster.com/stainless-steel-socket-head-screws/")
    by_part = {product.part_number: product for product in snapshot.products}

    assert "Group: 18-8 Stainless Steel" in by_part["90696A101"].context
    assert "Group: M14 x 2 mm" in by_part["90696A101"].context

    ranked = rank_products(
        "18-8 stainless steel socket head screw M14 x 2 mm thread 25 mm long pack of 5",
        snapshot.products,
        page_title=snapshot.title,
    )

    assert ranked[0].product.part_number == "90696A101"


def test_field_scoped_ranking_keeps_same_dimension_in_same_field():
    html = """
    <html>
      <head><title>Surface-Mount Hinges with Holes | McMaster-Carr</title></head>
      <body>
        <table>
          <thead>
            <tr><th>Door Leaf Ht.</th><th>Door Leaf Wd.</th><th>Overall Wd.</th><th></th></tr>
          </thead>
          <tr><td>6"</td><td>3/4"</td><td>1 1/2"</td><td><a href="/1586A31">1586A31</a></td></tr>
          <tr><td>6"</td><td>1 1/2"</td><td>3"</td><td><a href="/1586A33">1586A33</a></td></tr>
        </table>
      </body>
    </html>
    """
    snapshot = snapshot_from_html(html, "https://www.mcmaster.com/door-hinges/")
    by_part = {product.part_number: product for product in snapshot.products}
    by_part["1586A31"].name = 'Surface-Mount Hinge with Holes, Dull 304 Stainless Steel, Nonremovable Pin, 6" x 3/4" Door Leaf'
    by_part["1586A31"].sources.append("product_page")
    by_part["1586A33"].name = 'Surface-Mount Hinge with Holes, Dull 304 Stainless Steel, Nonremovable Pin, 6" x 1-1/2" Door Leaf'
    by_part["1586A33"].sources.append("product_page")
    ranked = rank_products(
        'door hinges. Surface-Mount Hinge with Holes, Dull 304 Stainless Steel, Nonremovable Pin, 6" x 3/4" Door Leaf. Door Leaf Ht.: 6"; Door Leaf Wd.: 3/4 "; Overall Wd.: 1 1/2 "',
        snapshot.products,
        page_title=snapshot.title,
    )

    assert ranked[0].product.part_number == "1586A31"


def test_model_number_is_strong_exact_signal():
    products = [
        ProductHit(
            part_number="20395A68",
            name='Cords for Measuring Tool Data Processors, Model 937387, 40" Long, 6-Pin x 10-Pin Connection',
            url="https://www.mcmaster.com/20395A68",
            context='Family: Mitutoyo 10-Pin Cables; Group: 40" Long; Electrical Connection: 6-Pin Mitutoyo Connector; Data Processor Connection: 10-Pin Mitutoyo Connector; Mfr. Model No.: 937387',
            sources=["link", "product_page"],
            confidence=1.0,
        ),
        ProductHit(
            part_number="20395A7",
            name='Cords for Measuring Tool Data Processors, Model 959149, 40" Long, Data Out Switch CX 10-Pin Connection',
            url="https://www.mcmaster.com/20395A7",
            context='Family: Mitutoyo 10-Pin Cables; Group: 40" Long; Electrical Connection: Mitutoyo Data Out Switch C; Data Processor Connection: 10-Pin Mitutoyo Connector; Mfr. Model No.: 959149',
            sources=["link", "product_page"],
            confidence=1.0,
        ),
    ]
    ranked = rank_products(
        'Cords for Measuring Tool Data Processors, Model 959149, 40" Long, Data Out Switch CX 10-Pin Connection. Connection: Mitutoyo Data Out Switch C x 10-Pin Mitutoyo Connector',
        products,
    )

    assert ranked[0].product.part_number == "20395A7"


def test_product_type_title_beats_same_range_for_accessory():
    products = [
        ProductHit(
            part_number="4996A74",
            name='Digital Caliper, 0" to 12" and 0 mm to 300 mm Measuring Ranges',
            url="https://www.mcmaster.com/4996A74",
            context='Family: Digital Calipers; Measuring Range Inch: 0" to 12"; Measuring Range Metric, mm: 0 to 300; Body Material: Stainless Steel',
            sources=["link", "product_page"],
            confidence=1.0,
        ),
        ProductHit(
            part_number="2231N13",
            name='Case for Starrett Calipers, with 0" to 12" and 0 mm to 300 mm Measuring Ranges',
            url="https://www.mcmaster.com/2231N13",
            context='Family: Starrett Caliper Cases; For Caliper Measurement Range: 0" to 12" , 0 mm to 300 mm; Material: Wood; For Mfr. Model No.: 120-12 , 120Z-12 , 798A-12/300',
            sources=["link", "product_page"],
            confidence=1.0,
        ),
    ]
    ranked = rank_products(
        'digital caliper. Case for Starrett Calipers, with 0" to 12" and 0 mm to 300 mm Measuring Ranges. Family: Digital Calipers; Group: Starrett; Material: Wood',
        products,
    )

    assert ranked[0].product.part_number == "2231N13"
