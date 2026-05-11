from mcmaster_navigator_mcp.extract import (
    extract_part_numbers,
    normalize_target,
    search_url,
    snapshot_from_html,
)
from mcmaster_navigator_mcp.models import FindPartsResult
from mcmaster_navigator_mcp.catalog_text import derive_search_queries, derive_search_query


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


def test_mcmaster_family_links_order_before_filter_links():
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

def test_snapshot_exposes_dynamic_table_schema_and_row_attributes():
    html = """
    <html>
      <head><title>Stainless Steel Socket Head Screws | McMaster-Carr</title></head>
      <body>
        <a href="/screws/thread-size~m14-2-mm/">M14 x 2 mm</a>
        <h2>Stainless Steel Socket Head Screws</h2>
        <table>
          <thead>
            <tr><th></th><th>Lg.</th><th>Pkg. Qty.</th><th></th><th>Pkg.</th></tr>
          </thead>
          <tr class="_stackPivotRow_q8hpp_63"><td colspan="5">18-8 Stainless Steel</td></tr>
          <tr class="_stackPivotRow_q8hpp_63"><td colspan="4">M14 x 2 mm</td></tr>
          <tr><td>25 mm</td><td>5</td><td class="_partNumberCell_krvpj_1"><a href="/90696A101">90696A101</a></td><td class="_priceCell_14fib_77">8.76</td></tr>
        </table>
      </body>
    </html>
    """
    snapshot = snapshot_from_html(html, "https://www.mcmaster.com/stainless-steel-socket-head-screws/")
    by_part = {product.part_number: product for product in snapshot.products}

    assert by_part["90696A101"].attributes["Lg."] == "25 mm"
    assert by_part["90696A101"].attributes["Pkg. Qty."] == "5"
    assert by_part["90696A101"].family == "Stainless Steel Socket Head Screws"
    assert by_part["90696A101"].groups == ["18-8 Stainless Steel", "M14 x 2 mm"]

    schema = snapshot.schemas[0]
    table = schema["tables"][0]
    row = table["rows"][0]
    assert table["columns"] == ["Lg.", "Pkg. Qty."]
    assert row["part_number"] == "90696A101"
    assert row["attributes"]["Lg."] == "25 mm"
    assert row["groups"] == ["18-8 Stainless Steel", "M14 x 2 mm"]


def test_schema_rows_include_section_header_groups_before_table_groups():
    html = """
    <html>
      <head><title>Eyebolts | McMaster-Carr</title></head>
      <body>
        <h2>Eyebolts—For Lifting</h2>
        <div class="_remainingOrg2PresentationHeader_11uzw_15">
          <span class="_subtableHeader_11uzw_69">Zinc-Plated Steel</span>
        </div>
        <table>
          <thead>
            <tr><th>Thread Size</th><th>Thread Lg.</th><th>Without Shoulder</th></tr>
          </thead>
          <tr class="_stackPivotRow_q8hpp_63"><td colspan="3">Closed Eye</td></tr>
          <tr>
            <td>3/8 "-16</td>
            <td>2 1/2 "</td>
            <td class="_partNumberCell_krvpj_1"><a href="/3013T101">3013T101</a></td>
          </tr>
        </table>
      </body>
    </html>
    """

    snapshot = snapshot_from_html(html, "https://www.mcmaster.com/eyebolts/")
    by_part = {product.part_number: product for product in snapshot.products}
    row = snapshot.schemas[0]["tables"][0]["rows"][0]

    assert by_part["3013T101"].groups == ["Zinc-Plated Steel", "Closed Eye"]
    assert row["groups"] == ["Zinc-Plated Steel", "Closed Eye"]
    assert "Group: Zinc-Plated Steel" in by_part["3013T101"].context


def test_schema_rows_keep_null_spec_cells_as_dynamic_attribute_values():
    html = """
    <html>
      <head><title>Timing Belt Pulleys | McMaster-Carr</title></head>
      <body>
        <h2>Timing Belt Pulleys</h2>
        <table>
          <thead>
            <tr><th>No. of Teeth</th><th>No. of Flanges</th><th></th><th>Each</th></tr>
          </thead>
          <tr>
            <td>60</td>
            <td class="_nullCell_19tvz_1">—</td>
            <td class="_partNumberCell_krvpj_1"><a href="/1375K158">1375K158</a></td>
            <td class="_priceCell_14fib_77">$17.82</td>
          </tr>
        </table>
      </body>
    </html>
    """

    snapshot = snapshot_from_html(html, "https://www.mcmaster.com/timing-belt-pulleys/")
    by_part = {product.part_number: product for product in snapshot.products}
    row = snapshot.schemas[0]["tables"][0]["rows"][0]

    assert by_part["1375K158"].attributes["No. of Flanges"] == "—"
    assert row["attributes"]["No. of Flanges"] == "—"
    assert "Each" not in row["attributes"]


def test_schema_extracts_linked_option_variants_as_dynamic_attributes():
    html = """
    <html>
      <head><title>Toggle Switches | McMaster-Carr</title></head>
      <body>
        <h2>Toggle Switches</h2>
        <table>
          <thead>
            <tr>
              <th>No. of Terminals</th>
              <th>Switch Designation</th>
              <th>Choose a Wire Connection</th>
              <th></th>
            </tr>
          </thead>
          <tr>
            <td>2</td>
            <td>SPST-NO</td>
            <td class="_easyToOrderCell_12opd_1">
              <a href="/7343K184-7343K185/">Quick-Disconnect Terminal</a>,
              <a href="/7343K184-7343K186/">Screw Terminal</a>
            </td>
            <td class="_partNumberCell_krvpj_1"><a href="/7343K184/">7343K184</a></td>
          </tr>
        </table>
      </body>
    </html>
    """
    snapshot = snapshot_from_html(html, "https://www.mcmaster.com/toggle-switches/")
    rows = {row["part_number"]: row for row in snapshot.schemas[0]["tables"][0]["rows"]}

    assert rows["7343K184"]["attributes"]["No. of Terminals"] == "2"
    assert rows["7343K185"]["attributes"]["Choose a Wire Connection"] == "Quick-Disconnect Terminal"
    assert rows["7343K186"]["attributes"]["Choose a Wire Connection"] == "Screw Terminal"
