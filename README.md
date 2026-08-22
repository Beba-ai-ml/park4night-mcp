# park4night-mcp

MCP server that exposes [Park4Night](https://park4night.com) campervan and motorhome spots to Claude (and any other Model Context Protocol client) — with a grid-sampling trick that returns 5-9x more results than a single API query.

> **Unofficial — not affiliated with Park4Night.** Uses publicly accessible website endpoints. Be respectful with rate limits.

---

## Key Features

- **search_places** -- find spots near GPS coordinates with adaptive grid sampling
- **search_along_route** -- discover overnight stops within a corridor between two points
- **get_reviews** -- fetch reviews/comments for any specific spot
- **Grid sampling** -- 1/5/9/13 query points based on radius, returns 5-9x more unique places
- **Async httpx** -- parallel queries, full search returns in 1-3s
- **Zero auth** -- works against the public unauthenticated endpoint, no API key

---

## Architecture

```
                          +-------------------------+
                          |  Claude / MCP client    |
                          +-----------+-------------+
                                      | stdio (MCP)
                                      v
                          +-------------------------+
                          |   server.py (FastMCP)   |
                          |   - 3 tools             |
                          |   - grid generator      |
                          |   - dedup by place_id   |
                          +-----------+-------------+
                                      | parallel httpx
                                      v
            +-------------------------+-------------------------+
            |                                                   |
            v                                                   v
  park4night.com/api/places/around          guest.park4night.com/.../commGet.php
  (base64-encoded JSON, ~100 cap)           (reviews, plain JSON)

  Grid sampling for search_places:

     radius < 15 km   ->  1 point   (center only)
     radius < 35 km   ->  5 points  (center + 4 cardinal at 60% radius)
     radius < 60 km   ->  9 points  (center + 8 points across 2 rings)
     radius >= 60 km  ->  13 points (center + 12 points across 3 rings)

  Each point queries the API in parallel; results deduplicated by place_id.
```

---

## Quick Start

```bash
git clone https://github.com/Beba-ai-ml/park4night-mcp.git
cd park4night-mcp
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

---

## Claude Desktop Config

Add to `claude_desktop_config.json` (location: `~/Library/Application Support/Claude/` on macOS, `%APPDATA%\Claude\` on Windows, `~/.config/Claude/` on Linux):

```json
{
  "mcpServers": {
    "park4night": {
      "command": "/absolute/path/to/park4night-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/park4night-mcp/server.py"]
    }
  }
}
```

Replace `/absolute/path/to/` with your actual clone path. Restart Claude Desktop after editing.

---

## Tools

| Tool | Parameters | Returns |
|---|---|---|
| `search_places` | `latitude` (float), `longitude` (float), `radius_km` (float, default `30.0`) | Up to 50 ranked spots within radius (sorted by rating). Each spot includes id, name, category, GPS, rating, reviews count, distance, city, country, amenities, URL. |
| `search_along_route` | `start_lat`, `start_lon`, `end_lat`, `end_lon` (floats), `step_km` (float, default `50.0`), `corridor_km` (float, default `20.0`) | Up to 50 spots grouped by route segment (sorted by progress along route, then rating). |
| `get_reviews` | `place_id` (int) | Up to 20 reviews with rating, username, date, comment text (English preferred, falls back to French). |

---

## Example Prompts

- *"Find free campervan spots near Lake Garda within 30 km."*
- *"Plan a route from Munich to Venice with overnight stops every 100 km."*
- *"Show me reviews for Park4Night place 12345."*
- *"What are the highest-rated motorhome areas in Provence?"*

---

## How It Works

### Grid Sampling

Park4Night's `/api/places/around` endpoint caps results at roughly 100 spots per query, regardless of the requested radius. For a 50 km radius around a popular region, this hides hundreds of valid spots.

This server samples up to 13 query points across the search area, runs all queries in parallel via `httpx.AsyncClient`, and deduplicates results by `place_id`. Net effect: in dense regions a 50 km query that would return ~100 spots from a single call returns ~580 unique spots after grid sampling.

The grid density adapts to the requested radius — small searches stay cheap (1 query), large searches get full coverage (13 queries).

### Response Decoding

The website API returns base64-encoded JSON (not the same as the guest API). The server decodes it, normalizes type codes (P, ACS, PN, etc.) into human-readable categories, strips HTML from descriptions, and extracts amenities into a flat comma-separated list.

### Tech Stack

- Python 3.11+
- `mcp` >= 1.26.0 (FastMCP)
- `httpx` >= 0.28.0
- Standard library only beyond that

---

## Security & Limitations

- **No secrets, no auth.** The server only reads public, unauthenticated Park4Night endpoints — there are no API keys or credentials anywhere in this repo.
- **Untrusted upstream content.** Spot descriptions and reviews are user-generated content from Park4Night and are passed to the LLM verbatim. Treat them as untrusted input (a possible prompt-injection vector), exactly as you would any other web content.
- **Rate-limit friendly.** `step_km` is floored at 5 km and the route point count is capped at 200, so a single call can't accidentally fan out into thousands of sequential requests against the upstream API.
- **Upstream bot filtering.** Park4Night rejects requests carrying httpx's default anonymous User-Agent with `403 Forbidden`, so the server sends an identifying User-Agent naming this client and linking the repository. If the endpoints start refusing again, the tools now say so explicitly instead of reporting `Found 0 spots` — an empty result means the area really is empty, and an API problem is labelled as one.

## License

MIT — see [LICENSE](LICENSE).
