#!/usr/bin/env python3
"""Park4Night MCP Server — search campervan spots via Park4Night API.

Uses the website API (park4night.com/api/places/around) which returns
more results than the guest API. Responses are base64-encoded JSON.
Grid-based querying multiplies coverage across the search area.
"""

import asyncio
import base64
import json
import math
import re
import sys
from mcp.server.fastmcp import FastMCP
import httpx

mcp = FastMCP(
    "park4night",
    instructions=(
        "Search for campervan/motorhome spots on Park4Night. "
        "User preferences: FREE spots only (unless explicitly asked for paid/camping). "
        "Prioritize spots near water (lakes, rivers, sea) or high in mountains with views."
    ),
)

API_AROUND = "https://park4night.com/api/places/around"
API_REVIEWS = "https://guest.park4night.com/services/V4.1/commGet.php"
TIMEOUT = 15.0
# park4night.com 403s the httpx default UA ("python-httpx/...") — needs a browser UA
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}

# Park4Night place type codes
TYPE_CODES = {
    "P": "Parking lot day/night",
    "CP": "Parking lot day/night",
    "ACS": "Motorhome area",
    "ACC_P": "Motorhome area",
    "ACC_PR": "Motorhome area (private)",
    "C": "Camping",
    "CC": "Camping",
    "CPCC": "Private car park for campers",
    "AR": "Rest area",
    "PN": "Surrounded by nature",
    "BP": "Off the beaten track",
    "AP": "Picnic area",
    "APN": "Picnic area",
    "PI": "Point of interest",
    "DS": "Service area (water/dump)",
    "PSS": "Service area (water/dump)",
    "ASS": "Service area (water/dump)",
    "EH": "Extra - homestays",
    "EF": "Extra - farm",
    "F": "On the farm",
    "FE": "On the farm",
    "CPJ": "Parking lot day only",
    "PJ": "Parking lot day only",
    "OR": "Overnight rest area",
}

SERVICE_LABELS = {
    "water": "water",
    "black_water": "black water dump",
    "grey_water": "grey water dump",
    "wc": "WC",
    "shower": "shower",
    "electricity": "electricity",
    "wifi": "WiFi",
    "trash": "trash",
    "laundry": "laundry",
    "swimming_pool": "pool",
    "bakery": "bakery",
}


def _strip_html(text: str) -> str:
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", text).strip()


def _format_place(p: dict) -> dict:
    """Format a place from the website API response."""
    # Extract type info
    ptype = p.get("type", {})
    type_code = ptype.get("code", "?")
    category = TYPE_CODES.get(type_code, ptype.get("label", type_code))

    # Extract services/amenities
    services = p.get("services", [])
    if isinstance(services, list) and services and isinstance(services[0], dict):
        amenities = [s.get("label", "") for s in services if s.get("label")]
    elif isinstance(services, list):
        amenities = [str(s) for s in services if s]
    else:
        amenities = []

    # Extract address
    addr = p.get("address", {})
    city = addr.get("city", "")
    country = addr.get("country", "")

    # Clean title
    title = _strip_html(p.get("title_short", "") or p.get("title", "") or "")

    place_id = p.get("id")
    rating = p.get("rating")
    num_reviews = p.get("review")

    return {
        "id": place_id,
        "name": title or (p.get("description") or "")[:80],
        "category": category,
        "latitude": p.get("lat"),
        "longitude": p.get("lng"),
        "rating": rating if rating else None,
        "num_reviews": num_reviews if num_reviews else None,
        "description": (_strip_html(p.get("description") or ""))[:300],
        "country": country,
        "city": city,
        "price": "free",  # website API doesn't include pricing in list view
        "amenities": ", ".join(amenities) if amenities else None,
        "is_pro": p.get("isPro", False),
        "url": f"https://park4night.com/en/place/{place_id}" if place_id else None,
    }


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in km between two GPS points."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _interpolate_points(lat1: float, lon1: float, lat2: float, lon2: float, step_km: float) -> list[tuple[float, float]]:
    """Generate intermediate GPS points along a straight line.

    step_km is floored at 5 km and the point count capped at 200 to avoid a
    runaway number of sequential API requests (and division-by-zero at 0).
    """
    step_km = max(step_km, 5.0)
    distance = _haversine(lat1, lon1, lat2, lon2)
    num_points = min(200, max(2, int(distance / step_km) + 1))
    points = []
    for i in range(num_points):
        t = i / (num_points - 1) if num_points > 1 else 0
        lat = lat1 + t * (lat2 - lat1)
        lon = lon1 + t * (lon2 - lon1)
        points.append((round(lat, 5), round(lon, 5)))
    return points


def _generate_grid(lat: float, lon: float, radius_km: float) -> list[tuple[float, float]]:
    """Generate a grid of query points to cover the search area."""
    points = [(lat, lon)]

    if radius_km < 15:
        return points

    km_per_deg_lat = 111.0
    km_per_deg_lon = 111.0 * math.cos(math.radians(lat))

    if radius_km < 35:
        rings = [0.6]
    elif radius_km < 60:
        rings = [0.4, 0.75]
    else:
        rings = [0.33, 0.6, 0.85]

    for pct in rings:
        offset_km = radius_km * pct
        dlat = offset_km / km_per_deg_lat
        dlon = offset_km / km_per_deg_lon
        points += [
            (lat + dlat, lon),
            (lat - dlat, lon),
            (lat, lon + dlon),
            (lat, lon - dlon),
        ]

    return [(round(p[0], 5), round(p[1], 5)) for p in points]


async def _fetch_around(client: httpx.AsyncClient, lat: float, lon: float, radius: float) -> list[dict]:
    """Fetch places from the website API (base64-encoded response)."""
    try:
        resp = await client.get(
            API_AROUND,
            params={"lat": lat, "lng": lon, "radius": radius},
        )
        resp.raise_for_status()
        decoded = base64.b64decode(resp.text)
        return json.loads(decoded)
    except Exception as e:
        print(f"[park4night] fetch_around failed at ({lat}, {lon}): {e}", file=sys.stderr)
        return []


@mcp.tool()
async def search_places(latitude: float, longitude: float, radius_km: float = 30.0) -> str:
    """Search for campervan/motorhome spots near a GPS location.

    Uses a grid of query points to find more places across the search area.
    Small radius = 1 query, medium = 5 queries, large = 9+ queries.

    Args:
        latitude: GPS latitude (e.g. 43.2965 for Nice, France)
        longitude: GPS longitude (e.g. 5.3698 for Nice, France)
        radius_km: Search radius in kilometers (default 30, max ~100)
    """
    grid = _generate_grid(latitude, longitude, radius_km)

    seen_ids = set()
    places = []

    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as client:
        tasks = [_fetch_around(client, lat, lon, radius_km) for lat, lon in grid]
        results = await asyncio.gather(*tasks)

    for raw_places in results:
        for p in raw_places:
            pid = p.get("id")
            if pid in seen_ids:
                continue
            try:
                plat = float(p.get("lat", 0))
                plon = float(p.get("lng", 0))
                dist = _haversine(latitude, longitude, plat, plon)
                if dist <= radius_km:
                    seen_ids.add(pid)
                    formatted = _format_place(p)
                    formatted["distance_km"] = round(dist, 1)
                    places.append(formatted)
            except (ValueError, TypeError):
                continue

    places.sort(key=lambda x: -(float(x.get("rating") or 0)))

    lines = [f"Found {len(places)} spots within {radius_km}km of ({latitude}, {longitude}) [queried {len(grid)} points]:\n"]
    for i, p in enumerate(places[:50], 1):
        rating = f"★{p['rating']}" if p.get("rating") else "no rating"
        reviews = f"({p['num_reviews']} reviews)" if p.get("num_reviews") else ""
        amenities = f"\n   Amenities: {p['amenities']}" if p.get("amenities") else ""
        lines.append(
            f"{i}. **{p['name'][:60]}** [{p['category']}]\n"
            f"   {rating} {reviews} | {p['distance_km']}km away | {p['price']}"
            f"{amenities}\n"
            f"   {p['city']}, {p['country']} | {p['url']}"
        )

    if len(places) > 50:
        lines.append(f"\n... and {len(places) - 50} more spots")

    return "\n".join(lines)


@mcp.tool()
async def search_along_route(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    step_km: float = 50.0,
    corridor_km: float = 20.0,
) -> str:
    """Search for campervan spots along a route between two points.

    Queries Park4Night at regular intervals along the route and collects spots
    within a corridor on each side.

    Args:
        start_lat: Starting point latitude
        start_lon: Starting point longitude
        end_lat: Ending point latitude
        end_lon: Ending point longitude
        step_km: Distance between search points along the route (default 50km, floored at 5km)
        corridor_km: How far from route to search (default 20km each side)
    """
    points = _interpolate_points(start_lat, start_lon, end_lat, end_lon, step_km)

    seen_ids = set()
    all_places = []

    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as client:
        for idx, (lat, lon) in enumerate(points):
            raw_places = await _fetch_around(client, lat, lon, corridor_km)

            for p in raw_places:
                pid = p.get("id")
                if pid in seen_ids:
                    continue

                try:
                    plat = float(p.get("lat", 0))
                    plon = float(p.get("lng", 0))
                    dist = _haversine(lat, lon, plat, plon)
                    if dist <= corridor_km:
                        seen_ids.add(pid)
                        formatted = _format_place(p)
                        formatted["route_segment"] = idx
                        formatted["distance_from_route_km"] = round(dist, 1)
                        all_places.append(formatted)
                except (ValueError, TypeError):
                    continue

    all_places.sort(key=lambda x: (x["route_segment"], -(float(x.get("rating") or 0))))

    total_dist = _haversine(start_lat, start_lon, end_lat, end_lon)

    lines = [
        f"Route: ({start_lat}, {start_lon}) -> ({end_lat}, {end_lon})",
        f"Distance: ~{round(total_dist)}km | Searched {len(points)} points every {step_km}km | Corridor: +/-{corridor_km}km",
        f"Found **{len(all_places)}** unique spots:\n",
    ]

    current_segment = -1
    count = 0
    for p in all_places:
        if count >= 50:
            break
        if p["route_segment"] != current_segment:
            current_segment = p["route_segment"]
            seg_lat, seg_lon = points[current_segment]
            pct = round(current_segment / max(len(points) - 1, 1) * 100)
            lines.append(f"\n--- ~{pct}% of route (near {seg_lat}, {seg_lon}) ---")

        rating = f"★{p['rating']}" if p.get("rating") else "no rating"
        reviews = f"({p['num_reviews']} reviews)" if p.get("num_reviews") else ""
        amenities = f" | {p['amenities']}" if p.get("amenities") else ""
        lines.append(
            f"- **{p['name'][:60]}** [{p['category']}]\n"
            f"  {rating} {reviews} | {p['distance_from_route_km']}km from route | {p['price']}{amenities}\n"
            f"  {p['city']}, {p['country']} | {p['url']}"
        )
        count += 1

    if len(all_places) > 50:
        lines.append(f"\n... and {len(all_places) - 50} more spots")

    return "\n".join(lines)


@mcp.tool()
async def get_reviews(place_id: int) -> str:
    """Get reviews/comments for a specific Park4Night spot.

    Args:
        place_id: The Park4Night place ID (from search results)
    """
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as client:
        resp = await client.get(
            API_REVIEWS,
            params={"lieu_id": place_id},
        )
        resp.raise_for_status()
        raw = resp.json()

    # Handle both list and dict responses
    if isinstance(raw, dict):
        reviews_data = raw.get("commentaires", raw.get("comments", []))
    elif isinstance(raw, list):
        reviews_data = raw
    else:
        reviews_data = []

    if not reviews_data:
        return f"No reviews found for place ID {place_id}"

    lines = [f"Reviews for place {place_id} ({len(reviews_data)} total):\n"]
    for i, r in enumerate(reviews_data[:20], 1):
        note = f"★{r.get('note')}/5" if r.get("note") else ""
        date = r.get("date", "")[:10]
        user = r.get("username") or r.get("utilisateur_creation") or "anonymous"
        text = (r.get("commentaire_en") or r.get("commentaire") or r.get("commentaire_fr") or "")[:200]
        lines.append(f"{i}. {note} by {user} ({date})\n   {text}")

    if len(reviews_data) > 20:
        lines.append(f"\n... and {len(reviews_data) - 20} more reviews")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")
