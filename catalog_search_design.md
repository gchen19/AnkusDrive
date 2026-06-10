# DriftPin Catalog Search — Design Proposal

*Extending the DriftPin MCP from "model & simulate" to "find a real part you can buy that meets the spec."*

## The opportunity

Today DriftPin generates and analyzes geometry. The next natural step in a designer's
loop is: *"I've defined a bearing pocket / fastener / gear envelope — does an off-the-shelf
part exist that fits, and where do I buy it?"* This is exactly the role DigiKey/Mouser/
JLCPCB APIs play for electronics. The mechanical world has the same plumbing — it's just
more fragmented and gated. The good news: the highest-value distributor (McMaster-Carr)
and the largest CAD aggregators all expose STEP files programmatically.

## Landscape: who has an API, and do they have STEP files?

| Source | API type | Keyword/spec search? | STEP files? | Access gate |
|---|---|---|---|---|
| **McMaster-Carr** | REST (Product Information API) | **No** — lookup by part #/URL only | **Yes** (3-D STEP + 2-D DWG + datasheet) | Client cert + approved account; per-part *subscription* model, daily add limits, CAD downloads rate-limited |
| **Misumi** | REST (`api.us.misumi-ec.com`) + configurator | Partial (catalog + configurator) | **Yes** (STEP, IGES, SAT) | Free account; CAD gen is per-config, not a clean public search API |
| **TraceParts** | Documented developer API (`developers.traceparts.com/v2`) | **Yes** | **Yes** — 1,730+ supplier catalogs | Partner/API key |
| **CADENAS 3Dfindit / PARTcommunity** | REST + geometric/parametric search | **Yes** — text, parametric (`D<12`), 3D-shape, 2D-sketch, photo | **Yes** — ~5,000 catalogs, 121 formats | Partner/API key. (Their *FreeCAD* addon exists but was archived/unmaintained May 2026 — a gap worth filling) |
| **Grainger / RS / Würth / Bossard / Fastenal** | Mostly eProcurement/PunchOut (cXML/OCI), some REST | Catalog-level | Varies; often only on web product pages | B2B account, partner onboarding |
| **Octopart / Nexar** | GraphQL (electronics) | Yes | N/A (electronics) | API key — relevant only if DriftPin ever touches PCB/enclosure work |

**Two structural facts that shape the design:**

1. **McMaster has no search endpoint.** You can only retrieve data for a part number you
   already know, *and* you must "subscribe" to that part first. So McMaster is a
   great *enrichment + CAD-fetch* source but a poor *discovery* source. Discovery has to
   come from an aggregator (TraceParts / CADENAS) or from a normalized local index.
2. **The aggregators (TraceParts, CADENAS) are the real "search engines"** for mechanical
   parts and already return STEP. They're the closest analog to Octopart for electronics.
   Both gate behind a partner API key.

## Ease-of-use comparison (onboarding vs. capability)

"Easier than McMaster" splits into two different questions, and the answer flips
depending on which you mean.

**Onboarding ease (can I just get a key and call it?)** — None of the mechanical sources
are truly self-serve. Unlike the electronics world (DigiKey, Mouser, Nexar/Octopart all
have instant free developer portals), every mechanical CAD-catalog API is gated behind a
partner/B2B relationship:

| Source | How you get access | Self-serve? | Real-time call ease once in |
|---|---|---|---|
| **McMaster** | Email `eprocurement@mcmaster.com`, get approved, receive **client cert + password** | No (approval) | Easy REST, but **mutual-TLS cert** adds setup friction |
| **TraceParts** | Fill out API-key request form; validation "can take several days," "you might be contacted" | No (sales-touch) | **Easiest to call** — plain Bearer token, interactive docs, `llms.txt` |
| **CADENAS 3Dfindit** | Create API keys in the portal, but real docs are behind support tickets | No (support-gated) | Workable, but thin public docs |
| **Misumi** | OAuth at `account.misumi-ec.com`; no public part-search dev portal | No | Mostly eProcurement + configurator, not a clean search API |
| **Grainger / RS / Würth / Bossard / Fastenal** | B2B account + PunchOut/cXML onboarding | No | eProcurement plumbing, heavier than REST |

**Capability ease (does the API actually do discovery + CAD?)** — Here **TraceParts is
clearly the easiest to build against.** Its documented v2/v3 surface includes the search
primitives McMaster lacks, plus CAD generation:

- `GET /v2/search/productlist`, `…/categorylist`, `…/partnumberlist` — **real catalog search**
- `GET /v2/search/partnumber/availability` — check a specific part
- `GET /v3/product/configure` + `POST /v3/product/updateconfiguration` — drive configurators
- `GET /v3/product/caddataavailability` + `POST /v3/product/cadrequest` — **get the STEP**
- It even publishes an `llms.txt` and OpenAPI specs — the most AI/automation-friendly of the bunch.

**Verdict:** McMaster is the easiest to *call once you have a part number* (single clean
REST API), but it can't search and the client-cert setup is fiddly. **TraceParts is the
easiest for DriftPin's actual job** — it's the only source with a documented, Bearer-token,
search-*and*-CAD REST API — but you pay for that with a slower, sales-gated onboarding.
CADENAS is comparable in capability but worse-documented. Misumi and the MRO distributors
are not realistic "easy" options for third-party search today.

> Bottom line: there is no free, instant, self-serve mechanical-parts API like the
> electronics ones. If onboarding friction is the priority, start with **McMaster**
> (part-number-in, STEP-out). If discovery capability is the priority, the least-effort
> *code* path is **TraceParts** — budget lead time for the API key.

## Proposed architecture: a pluggable `catalog` source layer

Mirror the way DigiKey-style integrations work, but abstract over the fragmentation with a
**source-adapter pattern** so DriftPin isn't coupled to any one distributor.

```
mcp__driftpin__catalog_search          ← unified search across enabled sources
mcp__driftpin__catalog_get             ← full spec sheet for one part (by source+part#)
mcp__driftpin__catalog_fetch_cad       ← pull STEP/DWG, cache locally, return handle
mcp__driftpin__catalog_import          ← fetch_cad + import into active document as a Part
mcp__driftpin__catalog_match_feature   ← "find parts that fit THIS hole/pocket/shaft"
mcp__driftpin__catalog_sources         ← list configured sources + auth status
```

Each source implements a small adapter interface:

```python
class CatalogSource(Protocol):
    name: str
    def search(self, query: SpecQuery) -> list[PartHit]: ...
    def get(self, part_number: str) -> PartRecord: ...
    def fetch_cad(self, part_number: str, fmt="STEP") -> Path: ...
```

Adapters at launch: `McMasterSource` (get + CAD only, no search), `TracePartsSource`
(search + CAD), `CadenasSource` (search + parametric + geometric). Sources are enabled via
config + credentials; `catalog_sources` reports which are live. This keeps the pure-Python
analysis subpackage clean and lets the worker import adapters lazily.

### The killer feature: spec-driven matching from live geometry

This is what makes DriftPin different from "a search box." Because DriftPin already knows
the geometry the user just built, `catalog_match_feature` can turn a *selected feature*
into a structured spec query automatically:

- User selects a bore + counterbore → DriftPin reads diameter, depth, and the
  existing `bounding_box` / `min_clearance` → emits a query like
  `bearing, bore=8mm, OD≤22mm, width≤7mm` → ranks hits by fit and price.
- User selects a tapped hole created by `add_thread` → query McMaster/aggregator for the
  matching screw, washer, and insert; return a buyable BOM line.
- After `catalog_import`, run the existing `interference_check` / `interface_align_check`
  to confirm the real part actually drops in — *verify before you buy*.

CADENAS's geometric ("find similar shape") and parametric (`D<12`) search map almost 1:1
onto this; for McMaster, DriftPin builds the query from its own thread/bearing tables and
only uses McMaster to confirm the part exists and grab the STEP.

## Ingest pipeline (STEP → DriftPin part)

1. `catalog_fetch_cad` downloads the STEP to a local cache keyed by `source:part#` (dedupe,
   respect rate limits — McMaster explicitly rate-limits CAD).
2. Import via existing FreeCAD STEP import → wrap as a managed Part with a stable handle.
3. Tag the object with provenance metadata: `source`, `part_number`, `price`, `datasheet
   URL`, `fetched_at`. This makes `bom_extract` produce a *purchasable* BOM — part numbers
   and prices, not just geometry.
4. Optional: store a lightweight proxy (bounding box + key mating faces via
   `publish_interface`) so assemblies stay fast and you only load full STEP on demand.

## Caching, licensing & rate-limit notes (important)

- **Respect the gates.** McMaster's terms tie CAD/data access to an approved account and
  per-part subscriptions; don't scrape product pages as a workaround. Build the adapter to
  fail gracefully to "manual part-number entry" when no credential is configured.
- **Cache aggressively, redistribute nothing.** Cache STEP/specs locally per user; never
  re-host distributor CAD. This both satisfies licensing and absorbs rate limits.
- **Credentials live outside the model.** Store API keys/certs in user config, not in the
  document; surface auth state through `catalog_sources`.

## Suggested phased rollout

- **Phase 1 — McMaster enrichment + CAD fetch.** Highest signal, cleanest API. `catalog_get`,
  `catalog_fetch_cad`, `catalog_import`, provenance-tagged `bom_extract`. Discovery is
  manual (paste a part #), but the import-and-verify loop is immediately useful.
- **Phase 2 — Real search via one aggregator** (TraceParts *or* CADENAS). Adds
  `catalog_search` and turns DriftPin into a discovery tool. CADENAS adds parametric +
  geometric search, which pairs best with `catalog_match_feature`.
- **Phase 3 — Feature-driven matching + buyable BOM.** `catalog_match_feature` wired to
  selected geometry, auto-`interference_check` on import, and a BOM that exports part
  numbers + live prices. This is the differentiated end state.
- **Phase 4 (optional) — eProcurement / more distributors.** Grainger/RS/Würth via
  PunchOut/cXML for users who need a specific supply chain; Octopart/Nexar if DriftPin
  ever spans PCB + enclosure.

## Open questions to decide before building

1. **Which aggregator for Phase 2** — TraceParts (broadest catalog) vs CADENAS (best search
   modalities, and an abandoned FreeCAD addon whose users are now stranded)?
2. **How much to lean on `match_feature`** — is auto-spec-from-geometry the headline
   feature, or a Phase-3 nicety?
3. **Credential UX** — bring-your-own-key per source, or a DriftPin-brokered partner key?
4. **BOM ambition** — stop at "here's the part + price," or go all the way to PunchOut cart
   hand-off?
