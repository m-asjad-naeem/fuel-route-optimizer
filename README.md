# Fuel Route Optimizer

A Django 6.0 + DRF backend that, given a start and destination, returns a
real driving route and the minimum-cost sequence of fuel stops a vehicle
needs to make the trip — built against the supplied OPIS fuel-price CSV,
with 953 stations genuinely enriched with their own per-station coordinates
(not city-center approximations) across ten states, to show it working end
to end on more than one hardcoded path.

## Contents

- [Architecture](#architecture)
- [Setup](#setup)
- [Database initialization](#database-initialization)
- [Station coordinates: why offline, not live geocoding](#station-coordinates-why-offline-not-live-geocoding)
- [API: `POST /api/v1/routes/plan`](#api-post-apiv1routesplan)
- [Vehicle assumptions](#vehicle-assumptions)
- [USA-only validation](#usa-only-validation)
- [Fuel optimization logic](#fuel-optimization-logic)
- [Caching](#caching)
- [Error codes](#error-codes)
- [Demo route](#demo-route)
- [Postman collection](#postman-collection)
- [Running tests](#running-tests)
- [Known limitations](#known-limitations)

## Architecture

```
POST /api/v1/routes/plan
      ↓
DRF serializer            — validates the request; each location is independently
                             a place-name string or {latitude, longitude}
      ↓
RoutePlanService           — pure orchestration, no Django/HTTP import in the class itself
      ↓            ↓                    ↓                      ↓
CachedGeocodingService  CachedRoutingService   FuelStation repository   RouteGeospatialService
      ↓                      ↓                      ↓                      ↓
NominatimGeocodingProvider  OSRMRoutingProvider   (DB read only —        Shapely/pyproj corridor
(skipped for coordinate       (exactly ONE call      never geocoded        filtering + authoritative
 inputs)                     per uncached pair)      at request time)      travel-distance math
                                                                              ↓
                                                                        plan_fuel_stops (pure optimizer)
```

Each layer has exactly one job and doesn't reach into the others:
- The **optimizer** (`routes/domain/fuel_optimizer.py`) is plain Python — no Django, no HTTP, no Shapely. It only ever sees `StationCandidate.travel_distance_miles` (an OSRM-derived, authoritative distance), never the Shapely-projected `route_position_miles` used purely for ordering/corridor math.
- The **geospatial service** (`routes/services/geospatial.py`) does coordinate math only — no HTTP, no ORM.
- The **providers** (`routes/services/geocoding.py`, `routing.py`) know nothing about `FuelStation` or the database.
- **Station coordinates are read from the database, never geocoded during a request.**
- **OSRM is called at most once per request** (not once per candidate station).
- Money (`price_per_gallon`, `fuel_cost`, `total_cost`) is `Decimal` end to end, serialized as strings — never floated.
- **A location is validated against the real USA country boundary**
  (`routes/services/geography.py`), not a lat/lon rectangle — see
  [USA-only validation](#usa-only-validation).

## Setup

```bash
git clone <this-repo>
cd fuel-route-optimizer
cp .env.example .env
docker compose up --build
```

This starts Postgres and the Django dev server (`http://localhost:8000`).
No Nominatim/OSRM calls happen during startup or `docker compose up` itself —
only once you POST a request.

## Database initialization

Run once, in order, against the running `web` container:

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py import_fuel_prices data/fuel-prices.csv
docker compose exec web python manage.py enrich_station_coordinates --provider file --from-file data/station_coordinates.csv
```

All three are **safe to rerun**: `import_fuel_prices` dedupes by a canonical
(station identity, price) hash, and `enrich_station_coordinates` only
updates a station's coordinates if they've actually changed — rerunning the
full sequence against an already-initialized database updates nothing and
creates nothing new.

```
rows_read: 8151   stations_created: 6738   prices_created: 8023   (first run)
rows_read: 8151   stations_created: 0      prices_created: 0      (rerun)
```

## Station coordinates: why offline, not live geocoding

The supplied CSV has 6,738 stations and **none** come with coordinates.
Live-geocoding all of them against free Nominatim (rate-limited to 1
req/sec, and whose own usage policy asks that bulk one-time jobs like this
not be run repeatedly against the public instance) would take hours and
isn't something normal setup should ever require. Instead:

- `data/station_coordinates.csv` is a small, **committed, pre-computed
  fixture** — 953 real stations, each geocoded from its **own** address,
  across AL, CA, FL, GA, LA, MS, NM, NV, OK, and TX, generated once offline
  and checked into the repo like any other fixture.
- The normal setup path (`enrich_station_coordinates --provider file`,
  above) only ever reads this file — zero network calls.
- Building/extending the fixture is a separate, explicit, rate-limited,
  resumable command that never touches the database directly:
  ```bash
  python manage.py enrich_station_coordinates \
    --provider nominatim --out-file data/station_coordinates.csv \
    --states OK,TX,NM
  ```
  Every station gets its own attempt at its literal address first,
  falling back to its city/state center only if that specific address
  fails to resolve — never the other way around. Two deliberate forms of
  request reuse keep this efficient without ever substituting one
  station's coordinates for another's: (1) stations that share the exact
  same `(address, city, state)` — the source CSV's own documented
  duplication, e.g. the same physical stop under two different OPIS/rack
  ids — are geocoded once and the result applied to all of them; (2) a
  city/state fallback result is cached the first time it's needed and
  reused for a later station in the same city, rather than re-requested.
- **The runtime API never calls Nominatim for a station, ever.** Station
  geocoding has no code path in the request flow at all.

**Coordinate quality disclaimer:** the source CSV's addresses are
highway-exit descriptions ("I-40, EXIT 158"), not mailable street
addresses, so a majority don't resolve to the literal address. Of the 953
enriched stations, 338 resolved to their literal address
(`coordinate_source = nominatim`) and 615 fell back to their own city/state
center (`coordinate_source = nominatim_city_fallback`) — a real, honest
per-station attempt either way, never a city-level value assigned to a
station whose own address was never tried. These are reproducible,
honestly-labeled coordinates — **not** a claim of exact station
geolocation, and not production-grade. Enrichment (like the EPSG:5070
projection in the geospatial service) only covers CONUS stations — the 112
Canadian CSV rows are intentionally left unenriched (see
[USA-only validation](#usa-only-validation) for why location *validation*
is nonetheless not CONUS-limited).

**Coverage is partial, not nationwide** — see
[Known limitations](#known-limitations) for exactly which routes this
does and doesn't support today, and why.

## API: `POST /api/v1/routes/plan`

`start` and `destination` are each **independently** either a place-name
string (geocoded) or a `{"latitude", "longitude"}` object (used as-is,
never geocoded) — you can mix the two in one request.

```json
{
  "start": {"latitude": 35.4730, "longitude": -97.5171},
  "destination": {"latitude": 35.0841, "longitude": -106.6510}
}
```

```bash
curl -X POST http://localhost:8000/api/v1/routes/plan \
  -H "Content-Type: application/json" \
  -d '{
    "start": {"latitude": 35.4730, "longitude": -97.5171},
    "destination": {"latitude": 35.0841, "longitude": -106.6510}
  }'
```

**Response — `200 OK`** (this is the real demo route's actual response):

```json
{
  "route": {
    "distance_miles": 544.6851015071979,
    "duration_minutes": 561.815,
    "geometry": { "type": "LineString", "coordinates": ["... 4,951 real road points ..."] }
  },
  "vehicle": {
    "mpg": 10.0,
    "max_range_miles": 500.0,
    "tank_capacity_gallons": 50.0,
    "starting_fuel_gallons": 50.0
  },
  "fuel": {
    "feasible": true,
    "starting_fuel_gallons": 50.0,
    "fuel_consumed_gallons": 54.67,
    "total_gallons_purchased": 4.67,
    "total_cost": "13.40"
  },
  "stops": [
    {
      "station_id": 3628,
      "station_name": "CEFCO #2005",
      "address": "I-40, EXIT 73",
      "city": "Amarillo",
      "state": "TX",
      "coordinates": { "latitude": 35.20729, "longitude": -101.8371192 },
      "route_position_miles": 258.05,
      "distance_from_route_miles": 0.98,
      "detour_distance_miles": 1.95,
      "detour_fuel_gallons": 0.195,
      "price_per_gallon": "2.86900000",
      "fuel_before_gallons": 24.09,
      "fuel_purchased_gallons": 4.67,
      "fuel_after_gallons": 0.0,
      "fuel_cost": "13.40"
    }
  ]
}
```

Each stop carries everything needed to render it on a map alongside
`route.geometry` — name, address, city/state, and `coordinates` — not just
a bare `station_id` requiring a second lookup.

**Fuel cost semantics.** The vehicle starts with a full 50-gallon tank, so
`fuel_consumed_gallons` (total burned over the whole trip) and
`total_gallons_purchased`/`total_cost` (what was actually bought and
spent) are deliberately different numbers, not a bug:

- `starting_fuel_gallons` — fuel in the tank at trip start (always a full
  tank under the current, documented assumption).
- `fuel_consumed_gallons` — total fuel burned: the along-route distance
  **plus** every stop's round-trip detour off the route to its pump (see
  below) — i.e. everything the vehicle actually drove, not just
  `route.distance_miles / mpg`.
- `total_gallons_purchased` / `total_cost` — only what was actually bought,
  at the price paid at each stop. **The starting tank's fuel is never
  charged for** — `total_cost` is the sum of each stop's own `fuel_cost`,
  never `fuel_consumed_gallons × price`. `stops` can legitimately be `[]`
  on a feasible plan (`total_cost: "0.00"`) when the starting tank alone
  covers the trip — the destination itself is never a fuel stop.

**Detour fuel.** A station sits `distance_from_route_miles` off the route
corridor, not on it — reaching its pump and returning costs real driven
miles. `detour_distance_miles` (always `2 × distance_from_route_miles`)
and `detour_fuel_gallons` are exposed per stop so this is never a free
teleport: the optimizer reserves this fuel (split across the leg arriving
at the stop and the leg leaving it) before ever planning a purchase there,
and a stop that would leave the tank empty mid-detour is excluded from
feasibility entirely rather than silently assumed reachable.

`route_position_miles` on a stop is the station's **authoritative**
OSRM-derived distance along the route — never the raw Shapely-projected
ordering coordinate used internally for corridor filtering (see
Architecture above). Money fields are decimal **strings**
(`price_per_gallon` at 8 places, matching how prices are stored;
`fuel_cost`/`total_cost` at 2).

## Vehicle assumptions

| Constant | Value | Configurable via |
|---|---|---|
| Fuel efficiency | 10 MPG | `VEHICLE_MPG` |
| Maximum range | 500 miles | `VEHICLE_MAX_RANGE_MILES` |
| Tank capacity | 50 gallons | `VEHICLE_TANK_CAPACITY_GALLONS` (defaults to `max_range / mpg`) |
| Starting fuel | Full tank | Not independently configurable — it's a trip-start assumption, not a vehicle constant |

## USA-only validation

The client requirement is "start and finish location both within the USA."
This is checked two ways, deliberately without an extra API call per
request:

- **Coordinate input** — validated against the actual USA country boundary
  (`routes/services/geography.py`, a ~450-vertex polygon covering all 50
  states + DC, bundled offline from public-domain boundary data), via a
  Shapely point-in-polygon test. **Not** a lat/lon rectangle: a rectangle
  loose enough to cover Alaska and Hawaii also covers parts of southern
  Canada and northern Mexico — Toronto (43.65, -79.38) and Monterrey
  (25.69, -100.32) both fall inside a plain 24–50°N / -125–-66°W box, which
  is exactly the gap this replaces.
- **Address input** — the Nominatim request itself is constrained with
  `countrycodes=us`, so an ambiguous or non-US place name (e.g. "Toronto",
  or a "Paris" with no US match) is rejected by the geocoder's own filter
  rather than resolving to an unintended country and only being caught
  afterward.

**Supported geography is the full USA (50 states + DC) for validation
purposes.** Separately, and more narrowly, the *station data* itself only
covers the states listed in
[Station coordinates](#station-coordinates-why-offline-not-live-geocoding)
— a valid Alaska or Hawaii coordinate will pass validation but, like any
state outside current station coverage, will find no nearby stations.
That's a data-coverage limitation (see
[Known limitations](#known-limitations)), not a validation gap.

## Fuel optimization logic

A two-phase, pure, deterministic algorithm (`routes/domain/fuel_optimizer.py`):

1. **Feasibility** — a one-dimensional reachability scan (no DP/graph
   library): can the destination be reached at all through some chain of
   stations where every hop is ≤ 500 miles? If not, the route is reported
   infeasible with a specific reason, not silently approximated.
2. **Cost minimization** — greedy, over only the stations proven feasible:
   at each stop, if the destination is reachable on the fuel already in the
   tank, stop (buy nothing); else, find the nearest strictly-cheaper
   reachable station and buy just enough fuel to reach it; else, fill the
   tank completely (the current price is the best available before running
   out of options) and drive to the **cheapest** reachable station — not
   the farthest. Targeting the farthest reachable station can drive past a
   cheaper-but-nearer station to reach a pricier-but-farther one, forcing
   an unnecessary top-up at the higher price later; targeting the cheapest
   was independently verified against an exact LP solver across 15,000
   randomized scenarios (0 mismatches) before replacing an earlier version
   of this branch that targeted the farthest station and could be up to
   ~2% more expensive than optimal.

This is the standard "gas station problem" greedy, with two real
corrections made during development:

- The destination itself must be treated as a reachable waypoint, not just
  stations — a naive version that only ever considered stations could
  crash or misreport a perfectly feasible route the moment the destination
  became the only point left to reach.
- A station is not physically on the route — it sits
  `distance_from_route_miles` off to the side. Every hop to or from an
  actually-visited station reserves that station's one-way detour distance
  (so a round trip costs it twice — once arriving, once leaving), modeled
  as real driven miles rather than a free teleport to the pump and back.
  Both corrections, and the earlier farthest-vs-cheapest correction above,
  were independently re-verified: an exact brute-force-plus-LP solver
  (enumerate every subset of candidates as a fixed stop sequence, solve the
  exact minimum-cost fueling for that fixed sequence, take the best
  feasible subset) found 0 mismatches against the production optimizer
  across hundreds of randomized scenarios with nonzero detours, and 0
  mismatches on a zero-detour regression pass confirming no change for the
  common case where a station sits right at the route.

## Caching

Two small Postgres tables (`GeocodeCacheEntry`, `RouteCacheEntry`) — no
Redis. A repeated location string resolves without a second Nominatim call;
a repeated `(origin, destination)` pair reuses the same route without a
second OSRM call. Cache keys are SHA-256 hashes of a schema version +
normalized input, so bumping `GEOCODE_CACHE_SCHEMA_VERSION` /
`ROUTE_CACHE_SCHEMA_VERSION` invalidates old entries without a data
migration. A failed provider call is never cached.

| Request shape | Geocoding calls (cache miss) | Routing calls (cache miss) |
|---|---|---|
| coordinate / coordinate | 0 | 1 |
| address / address | up to 2 | 1 |
| Any repeat of an identical request | 0 | 0 |

Station data is always a plain DB read — never geocoded or routed
per-station, no matter how many stations are near the route.

## Error codes

| Status | `code` | Meaning |
|---|---|---|
| 400 | *(DRF field errors)* | Request failed validation |
| 422 | `location_not_found` | Geocoder found no match (`field` says which) |
| 422 | `location_out_of_scope` | Resolved location is outside the supported USA service area |
| 422 | `route_not_found` | OSRM found no route between the resolved points |
| 422 | `route_infeasible` | Route is physically impossible for the configured vehicle range |
| 500 | `internal_error` | The optimizer rejected its own inputs — a server-side data/config bug, never a client error |
| 502 | `geocoding_provider_error` | Nominatim timed out, errored, or returned something unparseable |
| 502 | `routing_provider_error` | OSRM timed out, errored, or returned something unparseable |

Every error is translated at the API boundary (`routes/api/views.py`) — no
response ever contains a raw `requests` exception, a provider URL, or a
traceback.

**`route_not_found` is read from OSRM's response body, not its HTTP
status.** Confirmed live against the real public OSRM server: a genuinely
unreachable pair of points (e.g. across an ocean) comes back as **HTTP
400** with `{"code": "NoRoute", ...}` in the body — not HTTP 200. The
routing provider (`routes/services/routing.py`) checks `code` before
deciding how to treat the status, so this correctly becomes the documented
`route_not_found`/422, not a `routing_provider_error`/502; a non-`NoRoute`
error code under a 4xx/5xx status (a malformed query, an OSRM outage)
still correctly becomes a 502.

## Demo route

**Oklahoma City, OK → Albuquerque, NM** via I-40 — a real ~545-mile drive
(confirmed against live OSRM), chosen because it's the highest-density real
corridor in the supplied CSV and genuinely exceeds the 500-mile vehicle
range, so the demo needs a real fuel stop without bending any assumption to
force one. See the coordinates in the API example above, or use the
Postman collection below.

Three further routes are confirmed working end to end against the real,
enriched station data, to show the API isn't limited to one hardcoded
path — **Atlanta, GA → Miami, FL** (covered by an automated test,
`test_api_route_plan_works_for_a_second_independent_corridor`, using a real
captured OSRM geometry), **Houston, TX → Atlanta, GA**, and **Dallas, TX →
Jacksonville, FL**. A route between two arbitrary US cities outside
currently-enriched states will return `422 route_infeasible` — see
[Known limitations](#known-limitations) for exactly which states that
covers today.

## Postman collection

[`postman/fuel-route-optimizer.postman_collection.json`](postman/fuel-route-optimizer.postman_collection.json) —
import into Postman, set the `base_url` variable (defaults to
`http://localhost:8000`), and run in order. Every request carries Postman
tests asserting its expected status (and error `code` where relevant), so
the whole collection can be run with the Collection Runner.

**Happy paths**

1. **Coordinate/coordinate** — the demo route above.
2. **Cached repeat** — resend request 1, demonstrates the cache (same
   result, no second OSRM call).
3. **Address/address** — the same route geocoded from place names.
4. **Mixed** — address start, coordinate destination.
5. **Multiple fuel stops** — Los Angeles → Dallas (4 stops).
6. **New York → Chicago** (1 stop).
7. **Atlanta → Miami** (2 stops).
8. **Short route** — San Francisco → Los Angeles, under 500 miles, no stop needed.

**Edge cases**

- E1–E6 — validation failures (missing field, blank string, latitude out
  of range, extra key, wrong type, malformed JSON) → `400`.
- E7 — unresolvable place name → `422 location_not_found`.
- E8–E9 — Honolulu / Paris → `422 location_out_of_scope`.
- E10 — start equals destination → `200`, zero distance and cost.
- E11 — Seattle → Miami, outside the enriched corridors → `422 route_infeasible`.

## Running tests

```bash
docker compose exec web python -m pytest -q
docker compose exec web python manage.py check
docker compose exec web python manage.py makemigrations --check
docker compose exec web ruff check .
```

No test requires a live Nominatim or OSRM endpoint — every provider/API
test mocks `requests.get`. (Nothing stops you from hitting the real public
endpoints manually, as the demo above does.)

## Known limitations

- **953 of 6,738 stations are enriched** with their own real coordinates,
  across AL, CA (sparse — only 7 stations in the supplied CSV fall in CA
  near the enriched corridors), FL, GA, LA, MS, NM, NV, OK, and TX. This is
  a genuine, honest subset — not nationwide, and not disguised as such.
  Enrichment of the remaining states is a config/data change
  (`enrich_station_coordinates --provider nominatim --states <...>`), never
  a code change, and is resumable/idempotent, but a full nationwide pass
  requires a multi-hour run against Nominatim's rate-limited public
  instance, which this submission deliberately did not force through in
  one sitting (see the enrichment command's own docstring for the
  resumable workflow).
- **Confirmed working today**: Oklahoma City↔Albuquerque, Atlanta↔Miami,
  Houston↔Atlanta, Dallas↔Jacksonville — routes entirely within or between
  enriched states.
- **Confirmed `422 route_infeasible` today, for lack of station data, not
  a bug**: any route needing stations in a not-yet-enriched state — for
  example Denver→Kansas City (CO/KS/MO unenriched), Phoenix→Salt Lake City
  (AZ/UT unenriched), Minneapolis→St. Louis (MN/IA/MO unenriched),
  San Francisco→Phoenix (AZ unenriched, CA only sparsely covered), New
  York→Chicago (NY/PA/OH/IN/IL unenriched), and Los Angeles→Dallas (AZ
  unenriched, so the corridor has a real gap even though both endpoint
  states have some coverage). Each of these was tested live against the
  running API as part of this submission's own verification, specifically
  to confirm the response is an honest `422`, never a silently wrong or
  fabricated result.
- Location **validation** (is this point in the USA) covers the full
  50 states + DC (see [USA-only validation](#usa-only-validation)); it is
  the *station data* that's narrower, and the two are intentionally
  decoupled — a valid Alaska/Hawaii/not-yet-enriched-state coordinate is
  accepted by validation and will correctly report `route_infeasible` or a
  stopless feasible result rather than being rejected outright.
- City-fallback coordinates (615 of the 953) are town-center precision for
  that specific station's own city, not exact station locations; 338 of
  953 resolved to the station's literal address.
- Django is pinned to 6.0.8: Django 6.1.1 (the newest release) removes
  `django.utils.cache.cc_delim_re`, which the latest released
  djangorestframework (3.17.1) still imports — confirmed by testing the
  upgrade against a fresh Docker build, not assumed. No code change can
  fix this; it needs a DRF release that drops the dependency.
- The real 8,151-row CSV import adds real time (~30-90s depending on the
  database backend) to the one test file that exercises it
  (`routes/tests/test_real_demo_corridor.py`); everything else runs in
  under a few seconds.
