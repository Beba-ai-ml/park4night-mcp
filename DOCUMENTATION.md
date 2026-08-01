# park4night-mcp — Technical Documentation

## Project Overview

`park4night-mcp` is a single-file Python server that implements the Anthropic [Model Context Protocol](https://modelcontextprotocol.io/) and exposes three tools for searching campervan and motorhome spots on [Park4Night](https://park4night.com). It targets MCP clients such as Claude Desktop, Claude Code, and any other host capable of speaking the MCP stdio transport.

The server's main value-add is a **grid-sampling strategy**: Park4Night's underlying API silently caps single queries at roughly 100 results, so a single call against a 50 km radius in dense regions misses hundreds of spots. The server samples up to 13 grid points across the requested area, queries them in parallel, and deduplicates by place ID, returning roughly 5-9x more unique places.

The entire server lives in one file (`server.py`, ~378 lines). There are no databases, no caches, no auth. Python 3.11+ and two third-party dependencies (`mcp`, `httpx`) — that's it.

---

## Architecture

```
                          +-------------------------+
                          |  Claude / MCP client    |
                          +-----------+-------------+
                                      | stdio (MCP JSON-RPC)
                                      v
                          +-------------------------+
                          |   server.py (FastMCP)   |
                          |                         |
                          |  Tools:                 |
                          |   search_places         |
                          |   search_along_route    |
                          |   get_reviews           |
                          |                         |
                          |  Helpers:               |
                          |   _generate_grid        |
                          |   _interpolate_points   |
                          |   _haversine            |
                          |   _fetch_around         |
                          |   _format_place         |
                          |   _strip_html           |
                          +-----------+-------------+
                                      | parallel httpx requests
                                      v
            +-------------------------+-------------------------+
            |                                                   |
            v                                                   v
  park4night.com/api/places/around          guest.park4night.com/.../commGet.php
  (base64-encoded JSON, ~100 cap)           (reviews, plain JSON)
```

### Request flow for `search_places`

1. Client invokes the tool over stdio with `latitude`, `longitude`, `radius_km`.
2. `_generate_grid` produces 1, 5, 9, or 13 GPS points based on radius.
3. `asyncio.gather` runs `_fetch_around` against every point in parallel.
4. Each response is base64-decoded into a JSON list of place dicts.
5. Results are merged into a single dict keyed by `place_id` (dedup).
6. Each place is filtered by exact `_haversine` distance against the original center (grid points overshoot the radius, so spots outside the user's actual radius are dropped).
7. `_format_place` normalizes the schema (type codes -> labels, HTML strip, amenities flatten).
8. List is sorted by rating (desc), the top 50 are rendered as Markdown, returned as a single string.

### Request flow for `search_along_route`

1. `_interpolate_points` divides the great-circle line between start and end into segments of `step_km`.
2. For each segment center, query Park4Night with `corridor_km` as the radius (sequential, not parallel — each segment may overlap and parallel calls didn't yield meaningful speedups in testing).
3. Results are deduplicated globally by `place_id`.
4. Each spot is tagged with its `route_segment` index and `distance_from_route_km`.
5. Sorted by `(route_segment, -rating)` so the response reads as a journey from start to end.

### Request flow for `get_reviews`

1. Single GET to the legacy guest endpoint (`commGet.php`) with `lieu_id=<place_id>`.
2. Response is plain JSON (no base64). The server tolerates either a list or a dict shape (`commentaires` / `comments` keys).
3. Top 20 reviews are formatted with rating, username, date, and English comment (falls back to French).

---

## Module Reference

All code lives in `server.py`.

### MCP Tools

#### `search_places(latitude: float, longitude: float, radius_km: float = 30.0) -> str`

Search for campervan/motorhome spots near a GPS location.

- **Parameters:**
  - `latitude` -- GPS latitude in decimal degrees (e.g. `43.2965`)
  - `longitude` -- GPS longitude in decimal degrees (e.g. `5.3698`)
  - `radius_km` -- search radius in kilometers (default `30.0`, practical max ~100)
- **Returns:** human-readable Markdown string with up to 50 spots ranked by rating descending. Each entry includes name, category, rating, review count, distance, country/city, amenities, and Park4Night URL. If the result set exceeds 50, a "and N more spots" tail is appended.
- **Behavior:** uses `_generate_grid` to expand to 1/5/9/13 query points, deduplicates, and drops anything outside the exact radius via `_haversine`.

#### `search_along_route(start_lat, start_lon, end_lat, end_lon, step_km=50.0, corridor_km=20.0) -> str`

Find spots along a straight-line route between two GPS points.

- **Parameters:**
  - `start_lat`, `start_lon` -- start coordinates
  - `end_lat`, `end_lon` -- end coordinates
  - `step_km` -- distance between sample points along the route (default `50.0`)
  - `corridor_km` -- search radius at each sample point, also acts as corridor half-width (default `20.0`)
- **Returns:** Markdown string with route metadata (total distance, point count, corridor) followed by spots grouped by route progress (`~0%`, `~25%`, etc.). Capped at 50 entries.
- **Note:** uses straight-line interpolation, not real road routing. For most overnight-stop planning this is good enough; for tight mountain passes consider supplementing with a routing API.

#### `get_reviews(place_id: int) -> str`

Fetch reviews for a specific Park4Night spot.

- **Parameters:**
  - `place_id` -- the Park4Night place ID, as returned by `search_places` or visible in `https://park4night.com/en/place/{place_id}`
- **Returns:** Markdown string with up to 20 reviews. Each review shows rating (out of 5), username, ISO date (truncated to YYYY-MM-DD), and the comment text. English comment is preferred, falls back to original French.

### Internal Helpers

#### `_strip_html(text: str) -> str`

Removes all `<...>` tags from a string using a single regex. Park4Night embeds `<br>`, `<p>`, and the occasional inline style in titles and descriptions.

#### `_format_place(p: dict) -> dict`

Maps a raw place dict from the API response to a flat dict with stable field names: `id`, `name`, `category`, `latitude`, `longitude`, `rating`, `num_reviews`, `description`, `country`, `city`, `price`, `amenities`, `is_pro`, `url`. Type codes are looked up against `TYPE_CODES` (constants section). The `price` field is always `"free"` because the website API doesn't include pricing in the list view.

#### `_haversine(lat1, lon1, lat2, lon2) -> float`

Standard great-circle distance in kilometers, Earth radius hardcoded at 6371.0 km.

#### `_interpolate_points(lat1, lon1, lat2, lon2, step_km) -> list[tuple[float, float]]`

Linear interpolation between two GPS points, returning at least 2 points and roughly one point per `step_km`. Coordinates rounded to 5 decimal places.

#### `_generate_grid(lat, lon, radius_km) -> list[tuple[float, float]]`

Builds the sample grid. Always returns the center point. For `radius_km >= 15`, adds one ring of 4 cardinal-direction points. For `radius_km >= 35`, adds a second ring. For `radius_km >= 60`, adds a third ring. Per ring, the offset is computed in degrees-per-km using `cos(lat)` to compensate for the longitude convergence at higher latitudes.

#### `async _fetch_around(client, lat, lon, radius) -> tuple[list[dict], str | None]`

Single-call wrapper around `GET /api/places/around` with base64 decoding. Returns `(places, error)`. A non-200 status, a transport error, an undecodable body, or a non-list payload each yield `([], "<description>")` rather than raising, so one bad grid point doesn't break the whole batch — but the caller can still tell a failure apart from a genuinely empty area.

Callers aggregate the errors: if *every* query point failed they return an explicit "Park4Night API error" message, and if only some failed they prepend a `WARNING:` line to the results. Reporting an upstream outage as `Found 0 spots` is what previously made a total API block look like "this country has no spots".

### Constants

| Constant | Value | Purpose |
|---|---|---|
| `API_AROUND` | `https://park4night.com/api/places/around` | Main "places near coords" endpoint, returns base64-encoded JSON |
| `API_PLACE` | `https://park4night.com/api/places` | Reserved for single-place lookup (not currently called by any tool) |
| `API_REVIEWS` | `https://guest.park4night.com/services/V4.1/commGet.php` | Legacy reviews endpoint, returns plain JSON |
| `TIMEOUT` | `15.0` | httpx timeout in seconds, applied to every call |
| `HEADERS` | dict, 2 entries | Identifying `User-Agent` (client name + repo URL) and `Accept`; Park4Night 403s the default `python-httpx/x.y` agent |
| `TYPE_CODES` | dict, 24 entries | Maps Park4Night type codes (P, ACS, PN, etc.) to readable labels |
| `SERVICE_LABELS` | dict, 11 entries | Maps service codes to readable labels (currently unused — services come back from the API with `label` fields already, but kept for fallback) |

---

## Configuration Reference

The server reads no environment variables and has no config file. Everything is in the source.

To tune behavior, edit `server.py`:

| Setting | Location | Effect |
|---|---|---|
| Result cap (50) | hardcoded in `search_places` and `search_along_route` | Top-N spots in the rendered Markdown |
| Reviews cap (20) | hardcoded in `get_reviews` | Top-N reviews in the rendered Markdown |
| Grid breakpoints (15/35/60 km) | `_generate_grid` | Where the grid expands from 1 -> 5 -> 9 -> 13 points |
| Ring offset percentages | `_generate_grid` | How far ring points are placed (0.6, 0.4/0.75, 0.33/0.6/0.85 of radius) |
| Description truncation (300 chars) | `_format_place` | Max length of place description in output |
| HTTP timeout (15s) | `TIMEOUT` constant | Applied to all httpx calls |
| Sort criterion | end of each tool function | Currently rating-desc; swap to distance-asc or review-count-desc as needed |

---

## Known Limitations

- **Single-query cap.** The grid-sampling workaround mitigates but doesn't eliminate the underlying Park4Night cap. In extremely dense urban areas (Paris, Berlin) even 13 grid points may miss spots near grid boundaries.
- **No rate limiting.** The server fires up to 13 parallel HTTP requests on each `search_places` call. Park4Night has not (in testing) returned 429 responses, but heavy usage may. Consider adding `httpx.Limits` if you batch many searches in a loop.
- **Undocumented endpoint.** `/api/places/around` and the base64 encoding are not part of any public Park4Night API contract. The format may change without notice.
- **No real road routing.** `search_along_route` uses straight-line interpolation. For routes through complex geography (Alps, Norwegian fjords) the corridor will miss spots reachable by road but far from the bee-line.
- **No pricing data.** The list endpoint returns `price: "free"` for everything because the field isn't included in the around-search response. The detail endpoint (`API_PLACE`) likely has it but isn't currently called.
- **Reviews endpoint is legacy.** `commGet.php` is the older guest API. If Park4Night decommissions the guest service, `get_reviews` will need to migrate to the website API equivalent.
- **No retries.** A failed grid point is dropped (and reported in the output, but not retried). Adding `httpx-retries` or a manual retry loop would improve robustness in flaky network conditions.
- **Bot filtering.** Park4Night returns `403 Forbidden` to requests carrying httpx's default `python-httpx/x.y` User-Agent. The server sends an identifying `User-Agent` naming this client and linking the repository (`HEADERS`) — that is enough, no browser impersonation needed. If 403s return, the block has moved beyond User-Agent matching — likely TLS/JA3 fingerprinting or IP reputation, neither of which a header tweak can fix.
- **Result rendering is fixed.** Output is human-readable Markdown. Clients that want structured JSON would need a thin adapter — easy to add as a fourth tool.

---

## Development

Test the server locally without an MCP client:

```bash
source .venv/bin/activate
python server.py
# server is now waiting for MCP stdio messages on stdin
```

Quick smoke test of the helpers:

```python
from server import _generate_grid, _haversine, _interpolate_points

# Grid for 50 km around Nice
print(_generate_grid(43.7102, 7.2620, 50.0))  # 9 points

# Distance Nice -> Marseille
print(_haversine(43.7102, 7.2620, 43.2965, 5.3698))  # ~158 km

# Route points every 100 km
print(_interpolate_points(48.1351, 11.5820, 45.4408, 12.3155, 100.0))  # Munich -> Venice
```

For end-to-end debugging of MCP traffic, run Claude Desktop with `MCP_LOG_LEVEL=debug` or use the [MCP inspector](https://github.com/modelcontextprotocol/inspector).
