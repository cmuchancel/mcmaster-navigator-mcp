# Dynamic Catalog Grounding for Agentic Mechanical Part Sourcing

Authoring note: this is a manuscript draft, not a final submission. The benchmark
numbers in Sections 5 and 6 are copied from local benchmark artifacts. The BOM
demonstration in Section 7 is written in final-paper style but still contains
bracketed placeholders that must be replaced after the assembly demonstration is
actually run.

## Abstract

AI engineering agents can generate detailed textual descriptions of mechanical
components, but downstream procurement and CAD retrieval workflows typically
require vendor-specific part numbers. This creates a practical bottleneck: a
generated requirement such as "316 stainless surface-mount hinge with
nonremovable pin and 2-3/8 in by 1-3/16 in leaves" is not directly usable by a
product information API unless it is first grounded to a catalog part number. We
present an unofficial Model Context Protocol (MCP) server for headless
navigation of McMaster-Carr that converts sufficiently detailed textual part
descriptions into exact part-number sets. The system does not rely on a
hand-coded ontology of screw, bearing, switch, hose, or fitting attributes.
Instead, it renders live catalog pages, extracts the visible product-table
schema, asks a language model to map requested constraints onto live field names
and live values, and then applies deterministic row filtering. The result is a
cardinality-aware interface: one matching part is returned as `unique`, multiple
matching parts are returned as `ambiguous`, and unsatisfied requirements return
`unresolved`.

We evaluate the system on 250 exact part-recovery cases sampled across 25
McMaster-Carr catalog categories, 25 near-ambiguity cases where one
discriminator was intentionally omitted, 25 nonexistent cases with impossible
required constraints, and 25 broad ambiguity cases. In the final benchmark runs,
the system recovered the correct single part in 250/250 exact cases, returned
ambiguity instead of a false unique result in 25/25 near-ambiguity cases and
25/25 broad ambiguity cases, and returned unresolved in 25/25 nonexistent cases.
We further describe an assembly-level sourcing demonstration in which component
requirements are resolved to part numbers and then passed to a product
information API for product data, datasheets, CAD, and a generated BOM.

## 1. Introduction

Computer-aided engineering workflows increasingly include natural-language and
agentic interfaces: engineers ask for components, assemblies, test fixtures, or
design variants, and language models can produce plausible component
requirements. However, procurement and CAD retrieval still require exact catalog
identifiers. A part description is useful to a human, but a product information
API usually needs a part number.

This paper studies the missing retrieval layer between text-level engineering
intent and part-number-grounded product data. We focus on McMaster-Carr because
it is a widely used mechanical component catalog and because its official
Product Information API retrieves information, price, images, CAD, and
datasheets once a part number is already known. The API therefore solves a
downstream data-access problem, but not the upstream problem of selecting the
part number from a textual component description.

The central challenge is that industrial catalogs are not organized around a
single fixed schema. A screw might be filtered by material, thread size, length,
drive style, package quantity, and finish. A switch might be filtered by circuit,
terminal count, maintained or momentary action, panel thickness, voltage, and
terminal style. A conveyor roller, spring, hinge, tape, bearing, or filter has a
different set of discriminating fields. A general system cannot be built by
hard-coding every possible catalog attribute.

We present a dynamic-schema approach. The system renders the current catalog
page, extracts product rows and visible column names, summarizes the live schema,
uses an LLM only to align the user's requested constraints with those live
fields and values, and then filters rows programmatically. This distinction is
important: the LLM is not asked to hallucinate or rank part numbers. It is asked
to produce grounded matchers against fields that were actually present on the
page.

The contributions are:

1. A reusable MCP server that exposes headless McMaster-Carr navigation and
   exact-part resolution to external AI agents.
2. A dynamic schema extraction and filtering method that avoids a hand-coded
   catalog ontology.
3. A cardinality-aware output contract that returns `unique`, `ambiguous`, or
   `unresolved` rather than forcing a top-ranked answer.
4. A benchmark suite covering exact recovery, near ambiguity, broad ambiguity,
   and nonexistent requirements.
5. An assembly-level sourcing workflow that connects part-number discovery to
   product-data, CAD, datasheet, and BOM retrieval.

## 2. Background and Related Work

### 2.1 Model Context Protocol for engineering tools

The Model Context Protocol standardizes how LLM applications connect to external
context and tools. MCP servers can expose resources, prompts, and tools; tools
are the primitive used when a model needs to execute an action such as querying
a database, calling an API, or driving an external system. Our implementation
uses MCP as the integration layer so that the same part-sourcing capability can
be called by any compatible agent host.

MCP is useful here because part sourcing is not simply text generation. It
requires a persistent browser session, rendered-page inspection, navigation,
timeouts, and a structured result contract. These actions are better exposed as
tools than hidden inside a prompt.

### 2.2 McMaster-Carr product data

McMaster-Carr provides a Product Information API for approved customers. The
official API includes requests for product information, price, image retrieval,
CAD retrieval, and datasheet retrieval once a subscribed part number is known.
This makes part-number discovery the critical upstream step. Our system is
designed to fill that step and then hand part numbers to an approved product
data pipeline.

### 2.3 Existing McMaster-Carr agent tooling

A prior public repository, `mjbraun/mcmaster-agent`, exposes an MCP server for
searching McMaster-Carr and looking up product information from part numbers.
Its README describes product lookup, search, URL generation, SeleniumBase UC
mode, cookie persistence, and a limitation that a visible browser window may
briefly appear. That tool is useful for basic search and product lookup. The
work reported here differs in three ways: it is headless-only, it exposes a
stateful navigation and schema extraction interface, and its exact-part resolver
is designed to decide result cardinality by dynamically filtering live catalog
schemas.

### 2.4 Agent optimization

Agent optimization frameworks such as Agent Lightning are relevant but not
required for the core contribution. Agent Lightning decouples agent execution
from reinforcement-learning training and supports optimization of existing
agents. The part-sourcing task in this paper could provide a reward signal for
future prompt or policy optimization: reward exact unique recovery when the
description is sufficient, reward ambiguity when constraints intentionally
underdetermine a set, and penalize false unique selections. The current
implementation uses fixed prompts and deterministic filtering, leaving RL-based
prompt optimization as future work.

## 3. Problem Definition

Input: a textual mechanical component description, optionally with an additional
broad search hint. The description may contain product-family language,
materials, dimensions, capacities, finishes, standards, packaging, or model
numbers. It does not contain the target part number.

Output: a structured part-number result:

- `unique`: exactly one catalog part number satisfies all grounded constraints.
- `ambiguous`: multiple part numbers satisfy the grounded constraints.
- `unresolved`: no part number satisfies the required constraints, or the
  required constraints cannot be grounded to live catalog data.
- `error`: browser, network, model, or infrastructure failure.

The target behavior is not ranking. A ranked list is inappropriate when the
engineering question is exact procurement: if the description identifies one
part, return one part; if it identifies multiple parts, return the matching
set; if it identifies none, return no part.

## 4. System Design

### 4.1 MCP server surface

The package exposes a Python MCP server over stdio. The primary tools are:

- `mcmaster_find_exact_part`: resolve a detailed text description to a unique,
  ambiguous, unresolved, or error result.
- `mcmaster_extract_schema`: open or search a page and return dynamically
  extracted filters, table columns, row attributes, and part-number rows.
- `mcmaster_search`, `mcmaster_open`, `mcmaster_follow_link`,
  `mcmaster_current_page`, and `mcmaster_back`: provide headless stateful
  navigation through rendered pages.
- `mcmaster_find_parts`: broad search that returns part numbers found during
  rendered search/category navigation.
- `mcmaster_url`: generate McMaster-Carr product or search URLs without
  launching a browser.

The implementation is packaged as `mcmaster-navigator-mcp`, requires Python
3.11 or newer, and uses SeleniumBase UC mode with headless Chrome. OpenAI
credentials are supplied with the standard `OPENAI_API_KEY` environment
variable or explicit server flags. No API keys or local paths are embedded in
the package.

### 4.2 Headless browser and rendered-page extraction

McMaster-Carr pages are rendered web pages with product tables, category links,
filters, and product detail pages. The navigator opens search URLs, optionally
auto-drills through category links, and snapshots the rendered HTML. The
extractor returns:

- part numbers from links, image paths, and rendered HTML,
- product hits with family, group headings, selected options, row attributes,
  evidence text, source URL, and page URL,
- navigable links with text and URL,
- schema objects containing filters, table columns, and part-number rows.

The browser is isolated by default with a temporary Chrome profile. The server
serializes browser work through a single worker so that browser state remains
coherent across navigation calls. Browser logs are kept off MCP protocol stdout.

### 4.3 Dynamic exact-part resolution

The resolver follows this sequence:

1. Normalize the user description into broad search roots and literal
   constraints using an LLM.
2. Search the generated roots headlessly, then spend the page budget on relevant
   links from those rendered pages.
3. Extract rows from product hits and schema tables. Each row is represented as
   a part number plus live fields: family, groups, selected option, attributes,
   evidence, source, URL, and page URL.
4. Summarize the live schema as available field names and sample values.
5. Ask the LLM to map requested constraints to the live schema and to choose
   accepted live values. For example, a request for "door leaf width 1-3/16 in"
   may map to `attributes.Door Leaf Wd.` with accepted value `1 3/16 "`.
6. Apply the matchers with Python row filtering. The filtering code, not the
   LLM, determines the surviving part numbers.
7. If filtering conflicts with the live schema, ask the LLM for a repaired
   mapping from the extracted field/value summary.
8. Preserve broad ambiguity when the description only names a product family or
   underspecified taxonomy.
9. For a single remaining part, run a final strict verification check against
   the selected row. If required constraints are missing or contradicted, return
   `unresolved` instead of `unique`.

This design lets the system generalize across product families because the
catalog schema is discovered at runtime. There is no static list of all possible
attributes for screws, springs, switches, hinges, bearings, hose fittings, raw
materials, or other categories.

### 4.4 Assembly-level orchestration

At the assembly level, each line item is handled as an independent exact-part
resolution problem. A component requirement is sent to `mcmaster_find_exact_part`.
If the result is unique, the part number is forwarded to the product information
pipeline for product data, price, image, CAD, and datasheet retrieval. If the
result is ambiguous, the matching part numbers and unresolved discriminators are
returned to the engineer or to a higher-level agent for clarification. If the
result is unresolved, the line item is flagged as impossible or underspecified.

This line-item independence allows parallel sourcing agents. Compatibility
checking can be layered above the resolver, but the core tool intentionally
solves the narrower and measurable problem of finding the catalog part numbers
that match stated requirements.

## 5. Benchmark Methodology

### 5.1 Exact part recovery

The exact recovery benchmark begins with known part numbers sampled from
rendered McMaster-Carr search/category pages. Seed queries cover broad catalog
areas such as door hinges, toggle switches, welding clamps, socket head cap
screws, air filter cartridges, drawer slides, compression springs, cartridge
heaters, grease fittings, digital calipers, PVC tubing, timing belt pulleys,
eye bolts, aluminum bar, wire rope clips, bearings, guide rails, pneumatic
cylinders, double-sided tape, beakers, motors, and conveyor rollers.

For each sampled part number, the benchmark builds a textual description from
the rendered product page or product-table row. The part number is withheld from
the resolver. A case is counted correct if the returned set contains the target
part number and the resolver reports a unique single part.

Final exact-recovery run:

- Run directory: `benchmark_runs/llm_schema_250_general2`
- Cases: 250
- Categories represented: 25
- Model: `gpt-5.4-mini`
- Page budget: 8
- Auto-drill depth: 2
- Maximum schema rows: 700
- LLM search roots: 2

### 5.2 Near ambiguity

The near-ambiguity benchmark starts from successful exact cases, collects the
live schema rows around the target, then removes one discriminator from the
description so that two to four parts are expected to remain. A case passes when
the resolver returns `ambiguous`, does not select a unique part, includes the
original target part, includes all expected matching part numbers, and returns a
bounded number of candidates.

Final near-ambiguity run:

- Run directory: `benchmark_runs/near_ambiguity_25_v5`
- Cases: 25
- Expected matching set size: 2 to 4
- Pass maximum returned candidates: 8
- Model: `gpt-5.4-mini`

### 5.3 Nonexistent requirements

The nonexistent benchmark starts from exact descriptions and appends impossible
required constraints such as an unobtainable material, an impossible color, and
an impossible size. The case passes if the resolver does not return a unique
part number.

Final nonexistent run:

- Run directory: `benchmark_runs/nonexistent_25_strict_v2`
- Cases: 25
- Expected behavior: `unresolved`
- Model: `gpt-5.4-mini`

### 5.4 Broad ambiguity

The broad ambiguity benchmark gives only underspecified category queries such as
"double sided tape", "ball bearing", "door hinges", or "toggle switch". These
queries should not collapse to a single part number. A case passes if the system
returns `ambiguous`, returns more than one part number, and does not select a
unique part.

Final broad-ambiguity run:

- Run directory: `benchmark_runs/total_ambiguity_25_v1`
- Cases: 25
- Expected behavior: `ambiguous`
- Model: `gpt-5.4-mini`

## 6. Results

### 6.1 Summary

| Evaluation | Cases | Expected status | Pass criterion | Passed | Pass rate | Tokens | Mean seconds/case |
|---|---:|---|---|---:|---:|---:|---:|
| Exact single-part recovery | 250 | `unique` | Correct target returned as single part | 250/250 | 100% | 1,862,490 | 71.038 |
| Near ambiguity | 25 | `ambiguous` | Expected 2-4 part set returned, no false unique | 25/25 | 100% | 187,701 | 64.325 |
| Nonexistent requirements | 25 | `unresolved` | No unique part selected | 25/25 | 100% | 268,293 | 75.348 |
| Broad ambiguity | 25 | `ambiguous` | Multiple parts returned, no false unique | 25/25 | 100% | 90,950 | 50.654 |
| Total final reported benchmark set | 325 | mixed | Task-specific | 325/325 | 100% | 2,409,434 | - |

The exact-recovery benchmark returned `unique` for all 250 cases, with a median
per-case latency of 54.448 s, p90 latency of 85.960 s, p95 latency of 161.763 s,
and maximum latency of 466.125 s. Token use averaged 7,449.96 tokens per case,
with median 6,962.5, p90 10,563.9, p95 11,183.6, and maximum 18,300.

The near-ambiguity benchmark returned `ambiguous` for all 25 cases. Returned
candidate counts ranged from 2 to 4, with mean 2.24. The expected matching set
coverage was 1.0 in all cases.

The nonexistent benchmark returned `unresolved` for all 25 cases, with zero
false unique selections.

The broad-ambiguity benchmark returned `ambiguous` for all 25 cases, with zero
false unique selections. Returned candidate counts ranged from 2 to 456, with
median 86 and mean 128.88. This is the intended behavior for underspecified
category-level queries: the system should expose the remaining ambiguity rather
than silently choose a top candidate.

### 6.2 Category coverage

The 250 exact-recovery cases covered 25 catalog categories:

| Category | Cases | Unique exact recoveries |
|---|---:|---:|
| Adhesives | 8 | 8 |
| Bearings | 8 | 8 |
| Building & Grounds | 20 | 20 |
| Conveying | 8 | 8 |
| Electrical & Lighting | 6 | 6 |
| Fabricating | 19 | 19 |
| Fastening & Joining | 20 | 20 |
| Filtering | 21 | 21 |
| Furniture & Storage | 14 | 14 |
| Hand Tools | 8 | 8 |
| Hardware | 8 | 8 |
| Heating & Cooling | 8 | 8 |
| Lab Supplies | 8 | 8 |
| Linear Motion | 7 | 7 |
| Lubricating | 8 | 8 |
| Measuring & Inspecting | 8 | 8 |
| Motors | 8 | 8 |
| Pipe Tubing Hose Fittings | 8 | 8 |
| Plumbing & Janitorial | 8 | 8 |
| Pneumatics | 8 | 8 |
| Power Transmission | 8 | 8 |
| Pulling & Lifting | 8 | 8 |
| Raw Materials | 8 | 8 |
| Shipping | 8 | 8 |
| Suspending | 7 | 7 |

The absence of misses across these categories is evidence that runtime schema
extraction can handle substantially different product families. It is not proof
of complete coverage of every McMaster-Carr page or every possible human
description.

### 6.3 Failure mode checks

The ambiguity and nonexistent experiments are important because a system that
always returns a top-ranked answer would look successful on exact cases while
still being unsafe for engineering procurement. The benchmark therefore tests
for false uniqueness directly. Across the final 75 non-exact cases, the system
made zero false unique selections.

This supports the central design choice: exact part sourcing should be framed as
constraint satisfaction over live catalog rows, not as generic ranked search.

## 7. Assembly-Level BOM Demonstration

This section is reserved for the assembly demonstration. The prose is written in
the form intended for the final paper, but the bracketed fields must be replaced
with the actual demonstration artifacts.

We demonstrate the end-to-end workflow on a [TODO: assembly name] assembly with
[TODO: number] purchasable components. The input is a structured assembly
description containing one line per required off-the-shelf component. Each line
contains a human-readable requirement, quantity, and any functional constraints
needed for part selection. The sourcing system resolves each component line
independently with `mcmaster_find_exact_part`. Unique results are passed to the
McMaster-Carr Product Information API pipeline to retrieve product metadata,
datasheets, CAD files, images, and price where available. Ambiguous and
unresolved lines are preserved in the BOM with their candidate sets and
resolution status.

### 7.1 Demonstration input

The assembly input should be stored as a reproducible artifact, for example:

- Assembly file: `[TODO: path to assembly specification JSON/SysML/CSV]`
- BOM output: `[TODO: path to generated BOM CSV]`
- CAD bundle: `[TODO: path to generated CAD artifact directory or zip]`
- Product-data output: `[TODO: path to product API JSON records]`

Suggested input schema:

| Field | Meaning |
|---|---|
| `line_id` | Stable assembly line-item identifier |
| `quantity` | Required quantity in the assembly |
| `description` | Textual component requirement sent to the resolver |
| `role` | Functional role in the assembly |
| `required` | Whether the line must be sourced for assembly completion |

### 7.2 Demonstration result table

| Line | Quantity | Component requirement | Resolver status | Part number(s) | Product API artifacts | Notes |
|---|---:|---|---|---|---|---|
| [TODO] | [TODO] | [TODO] | `unique` | [TODO] | product, CAD, datasheet | [TODO] |
| [TODO] | [TODO] | [TODO] | `unique` | [TODO] | product, CAD, datasheet | [TODO] |
| [TODO] | [TODO] | [TODO] | `ambiguous` | [TODO: candidate set] | not fetched until disambiguated | [TODO] |
| [TODO] | [TODO] | [TODO] | `unresolved` | none | none | [TODO] |

### 7.3 Demonstration metrics to report

The final paper should report:

- number of assembly line items,
- number and percentage resolved as `unique`,
- number and percentage returned as `ambiguous`,
- number and percentage returned as `unresolved`,
- total part numbers returned,
- number of product API records retrieved,
- number of CAD files retrieved,
- number of datasheets retrieved,
- wall-clock time,
- token use,
- final BOM path,
- final CAD/data bundle path.

Final-form result sentence:

> For the [TODO: assembly name] demonstration, the system resolved [TODO:
> unique count]/[TODO: total] line items to unique McMaster-Carr part numbers,
> returned [TODO: ambiguous count] ambiguous line items for clarification, and
> returned [TODO: unresolved count] unresolved line items. The downstream
> product-data pipeline retrieved [TODO: product records] product records,
> [TODO: CAD count] CAD files, and [TODO: datasheet count] datasheets, producing
> a BOM at [TODO: artifact path].

## 8. Discussion

### 8.1 Why dynamic schemas matter

The benchmark covers product families whose discriminating attributes differ
substantially. A static parser with a fixed list of fields would need to know
the domain-specific names and value formats for hinges, switches, bearings,
adhesives, filters, raw materials, pneumatic cylinders, motors, and conveyor
rollers. The dynamic approach avoids that requirement. The system only needs a
general row representation and a way to align user constraints with fields that
are currently visible on the page.

### 8.2 Why cardinality matters

Procurement workflows need faithful uncertainty. Returning a single best guess
for "ball bearing" is worse than returning many candidates, because the
description does not specify shaft diameter, housing diameter, width, seal type,
load capacity, or material. The ambiguity experiments show that the resolver can
avoid false precision in both broad and near-exact cases.

### 8.3 Integration with product-data APIs

Once part numbers are known, product-data retrieval is a conventional API task.
The official McMaster-Carr Product Information API includes endpoints for
product information, price, images, CAD, and datasheets. This separation lets
the proposed MCP server remain focused on the hard upstream retrieval problem:
finding the part numbers from text and navigation.

### 8.4 Prompt optimization opportunity

The benchmark logs include status, returned counts, target recovery, LLM token
use, pages visited, latency, and filter traces. These artifacts define natural
reward signals for prompt optimization or reinforcement learning. A future
Agent-Lightning-style loop could optimize the normalization and schema-mapping
prompts while leaving browser navigation and deterministic filtering unchanged.
The reward should preserve cardinality semantics: unique exact recovery is
rewarded only when exactly one correct part is returned; ambiguous cases are
rewarded when the matching set is returned without false uniqueness; nonexistent
cases are rewarded when no part is selected.

## 9. Limitations

The current benchmark uses descriptions generated from rendered catalog rows and
product pages. These descriptions are specific and often include labels such as
family, group, selected option, and attribute names. This is a valid test of
schema grounding and exact retrieval, but it is not the same as testing noisy
human design notes.

The system depends on rendered website structure. If McMaster-Carr changes page
layout, table markup, anti-automation behavior, or part-number presentation,
extraction may require maintenance.

The system is not affiliated with McMaster-Carr and should be used in accordance
with applicable terms and approved API access. The official product information
API requires approved customer access and subscription to product information.

Latency is dominated by headless browsing. The mean exact-recovery latency in
the final run was 71.038 seconds per case. Parallel line-item sourcing can
reduce assembly-level wall-clock time, but single-line resolution remains slow
relative to a pure API call.

The benchmark demonstrates coverage over 325 final reported cases, not over the
entire catalog. The strongest defensible claim is that dynamic schema extraction
and deterministic filtering can recover exact part numbers across diverse
catalog families when descriptions contain sufficient identifying information.

## 10. Conclusion

This work turns a practical procurement bottleneck into a measurable tool
interface. A text-to-design or text-to-assembly agent can describe a required
component, but a downstream product information API needs a part number. The
proposed MCP server bridges that gap by headlessly navigating McMaster-Carr,
extracting live product schemas, grounding textual constraints to live fields,
and filtering rows deterministically. In final benchmark runs, the system
recovered 250/250 exact part numbers across 25 catalog categories and avoided
false unique selections in 75 ambiguity and nonexistent cases. The assembly
demonstration will complete the story by showing how part-number discovery feeds
directly into BOM, CAD, datasheet, and product-data generation.

## Reproducibility Artifacts

Core package:

- `src/mcmaster_navigator_mcp/server.py`: MCP tool definitions and stdio server.
- `src/mcmaster_navigator_mcp/navigator.py`: headless browser navigation.
- `src/mcmaster_navigator_mcp/extract.py`: rendered HTML, product, link, and
  schema extraction.
- `src/mcmaster_navigator_mcp/schema_resolver.py`: dynamic exact-part resolver.

Benchmark scripts:

- `benchmarks/mcmaster_retrieval_benchmark.py`: seed collection and exact
  recovery benchmark.
- `benchmarks/near_ambiguity_benchmark.py`: near-ambiguity generation and
  scoring.
- `benchmarks/negative_ambiguity_benchmark.py`: nonexistent and broad ambiguity
  generation and scoring.
- `benchmarks/analyze_run.py`: exact-run analysis summary and per-category
  metrics.

Final benchmark artifacts:

- `benchmark_runs/llm_schema_250_general2/summary.json`
- `benchmark_runs/llm_schema_250_general2/analysis_summary.json`
- `benchmark_runs/llm_schema_250_general2/category_metrics.csv`
- `benchmark_runs/llm_schema_250_general2/case_metrics.csv`
- `benchmark_runs/near_ambiguity_25_v5/summary.json`
- `benchmark_runs/nonexistent_25_strict_v2/summary.json`
- `benchmark_runs/total_ambiguity_25_v1/summary.json`

## References

[1] Model Context Protocol specification. https://modelcontextprotocol.io/specification/2024-11-05/index

[2] Model Context Protocol tools specification. https://modelcontextprotocol.io/specification/draft/server/tools

[3] McMaster-Carr Product Information API. https://www.mcmaster.com/help/api/

[4] mjbraun/mcmaster-agent. https://github.com/mjbraun/mcmaster-agent

[5] X. Luo, Y. Zhang, Z. He, Z. Wang, S. Zhao, D. Li, L. K. Qiu, and Y. Yang,
"Agent Lightning: Train ANY AI Agents with Reinforcement Learning," arXiv:
2508.03680, 2025. https://arxiv.org/abs/2508.03680

