#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27.0",
# ]
# ///
"""
Sydney Tolls — check toll road prices and calculate route toll costs.

Zero-config: works immediately with no API keys.

Usage:
    uv run tolls.py                                  # list all toll roads
    uv run tolls.py --road "M2"                      # specific toll road
    uv run tolls.py --from "Parramatta" --to "Sydney Airport"  # route tolls
    uv run tolls.py --vehicle motorcycle --time peak  # filter by vehicle/time
    uv run tolls.py --all                             # list all toll roads
"""

import argparse
import asyncio
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Location:
    lat: float
    lng: float
    city: str
    state: str
    postcode: str
    country: str
    method: str  # how we detected it


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

CACHE_DIR = Path.home() / ".config" / "sydney-tolls"
CACHE_TTL_SECONDS = 86400  # 24 hours — toll prices change infrequently


def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{key}.json"


def cache_get(key: str) -> dict | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if time.time() - data.get("_cached_at", 0) < CACHE_TTL_SECONDS:
            return data.get("payload")
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def cache_set(key: str, payload: dict) -> None:
    path = _cache_path(key)
    path.write_text(json.dumps({"_cached_at": time.time(), "payload": payload}))


# ---------------------------------------------------------------------------
# Geolocation (for --from/--to route calculation)
# ---------------------------------------------------------------------------

AU_STATES = {
    "New South Wales": "NSW",
    "Victoria": "VIC",
    "Queensland": "QLD",
    "South Australia": "SA",
    "Western Australia": "WA",
    "Tasmania": "TAS",
    "Northern Territory": "NT",
    "Australian Capital Territory": "ACT",
    "NSW": "NSW",
    "VIC": "VIC",
    "QLD": "QLD",
    "SA": "SA",
    "WA": "WA",
    "TAS": "TAS",
    "NT": "NT",
    "ACT": "ACT",
}

NOMINATIM_HEADERS = {"User-Agent": "sydney-tolls-cli/1.0"}


async def _geocode_forward(
    client: "httpx.AsyncClient", query: str
) -> Location | None:
    """Forward geocode via Nominatim /search — convert place name to coords."""
    try:
        resp = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": query,
                "countrycodes": "au",
                "format": "jsonv2",
                "limit": 1,
                "addressdetails": 1,
            },
            headers=NOMINATIM_HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data:
            return None
        result = data[0]
        addr = result.get("address", {})
        suburb = addr.get("suburb") or addr.get("town") or addr.get("city") or ""
        state_raw = addr.get("state", "")
        state = AU_STATES.get(state_raw, state_raw)
        return Location(
            lat=float(result["lat"]),
            lng=float(result["lon"]),
            city=suburb,
            state=state,
            postcode=addr.get("postcode", ""),
            country=addr.get("country", "Australia"),
            method="nominatim-forward",
        )
    except Exception:
        return None


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Toll road data (Sydney, NSW — prices as of March 2026)
# ---------------------------------------------------------------------------

TOLL_ROADS = [
    {
        "name": "Sydney Harbour Bridge",
        "operator": "Transport for NSW",
        "direction": "Southbound only (free northbound)",
        "toll_points": [{"name": "Sydney Harbour Bridge", "lat": -33.8523, "lng": 151.2108}],
        "prices": {
            "car": {"peak": 4.00, "off_peak": 3.00, "weekend": 3.00},
            "motorcycle": {"peak": 0, "off_peak": 0, "weekend": 0},
            "heavy": {"peak": 8.00, "off_peak": 6.00, "weekend": 6.00},
        },
        "notes": "Free for motorcycles. Peak: Mon-Fri 6:30-9:30am, 4-7pm.",
    },
    {
        "name": "Sydney Harbour Tunnel",
        "operator": "Transport for NSW",
        "direction": "Southbound only (free northbound)",
        "toll_points": [{"name": "Sydney Harbour Tunnel", "lat": -33.8580, "lng": 151.2110}],
        "prices": {
            "car": {"peak": 4.00, "off_peak": 3.00, "weekend": 3.00},
            "motorcycle": {"peak": 0, "off_peak": 0, "weekend": 0},
            "heavy": {"peak": 8.00, "off_peak": 6.00, "weekend": 6.00},
        },
        "notes": "Free for motorcycles. Same pricing as Harbour Bridge.",
    },
    {
        "name": "M2 Hills Motorway",
        "operator": "Transurban",
        "direction": "Both directions",
        "toll_points": [
            {"name": "M2 main toll point", "lat": -33.7637, "lng": 151.0014},
            {"name": "Lane Cove Tunnel connector", "lat": -33.8050, "lng": 151.1470},
        ],
        "prices": {
            "car": {"peak": 8.49, "off_peak": 5.77, "weekend": 5.77},
            "motorcycle": {"peak": 3.14, "off_peak": 2.14, "weekend": 2.14},
            "heavy": {"peak": 16.98, "off_peak": 11.54, "weekend": 11.54},
        },
        "notes": "Peak: Mon-Fri 6:30-9:30am, 4-7pm. E-TAG or cash/license plate matching.",
    },
    {
        "name": "M4 Motorway (WestConnex)",
        "operator": "Transurban (WestConnex)",
        "direction": "Both directions",
        "toll_points": [
            {"name": "M4 Parramatta", "lat": -33.8130, "lng": 151.0120},
            {"name": "M4 Homebush", "lat": -33.8650, "lng": 151.0780},
        ],
        "prices": {
            "car": {"peak": 9.44, "off_peak": 6.42, "weekend": 6.42},
            "motorcycle": {"peak": 3.49, "off_peak": 2.38, "weekend": 2.38},
            "heavy": {"peak": 18.88, "off_peak": 12.84, "weekend": 12.84},
        },
        "notes": "Distance-based tolling. Prices shown are maximum. Peak: Mon-Fri 6:30-9:30am, 4-7pm.",
    },
    {
        "name": "M5 East Motorway",
        "operator": "Transurban",
        "direction": "Both directions",
        "toll_points": [{"name": "M5 East", "lat": -33.9350, "lng": 151.1570}],
        "prices": {
            "car": {"peak": 5.67, "off_peak": 3.85, "weekend": 3.85},
            "motorcycle": {"peak": 2.10, "off_peak": 1.43, "weekend": 1.43},
            "heavy": {"peak": 11.34, "off_peak": 7.70, "weekend": 7.70},
        },
        "notes": "M5 cashback scheme: NSW rego cars get tolls refunded (apply via Service NSW).",
    },
    {
        "name": "M5 South-West Motorway",
        "operator": "Interlink Roads",
        "direction": "Both directions",
        "toll_points": [{"name": "M5 SW toll point", "lat": -33.9440, "lng": 150.8680}],
        "prices": {
            "car": {"peak": 5.30, "off_peak": 3.60, "weekend": 3.60},
            "motorcycle": {"peak": 1.96, "off_peak": 1.33, "weekend": 1.33},
            "heavy": {"peak": 10.60, "off_peak": 7.20, "weekend": 7.20},
        },
        "notes": "M5 cashback scheme applies.",
    },
    {
        "name": "M7 Motorway",
        "operator": "Transurban",
        "direction": "Both directions",
        "toll_points": [
            {"name": "M7 North", "lat": -33.7340, "lng": 150.8830},
            {"name": "M7 South", "lat": -33.9440, "lng": 150.8680},
        ],
        "prices": {
            "car": {"peak": 9.15, "off_peak": 9.15, "weekend": 9.15},
            "motorcycle": {"peak": 3.39, "off_peak": 3.39, "weekend": 3.39},
            "heavy": {"peak": 18.30, "off_peak": 18.30, "weekend": 18.30},
        },
        "notes": "Distance-based (38.5c/km car, max shown). No peak/off-peak — same price all times.",
    },
    {
        "name": "M8 Motorway (WestConnex)",
        "operator": "Transurban (WestConnex)",
        "direction": "Both directions",
        "toll_points": [{"name": "M8 tunnel", "lat": -33.9370, "lng": 151.1540}],
        "prices": {
            "car": {"peak": 7.65, "off_peak": 5.20, "weekend": 5.20},
            "motorcycle": {"peak": 2.83, "off_peak": 1.92, "weekend": 1.92},
            "heavy": {"peak": 15.30, "off_peak": 10.40, "weekend": 10.40},
        },
        "notes": "Distance-based tolling. Prices shown are maximum. Peak: Mon-Fri 6:30-9:30am, 4-7pm.",
    },
    {
        "name": "Eastern Distributor",
        "operator": "Transurban",
        "direction": "Northbound only (free southbound)",
        "toll_points": [{"name": "Eastern Distributor", "lat": -33.8710, "lng": 151.2220}],
        "prices": {
            "car": {"peak": 8.95, "off_peak": 6.08, "weekend": 6.08},
            "motorcycle": {"peak": 3.31, "off_peak": 2.25, "weekend": 2.25},
            "heavy": {"peak": 17.90, "off_peak": 12.16, "weekend": 12.16},
        },
        "notes": "Northbound toll only. Free southbound.",
    },
    {
        "name": "Cross City Tunnel",
        "operator": "Transurban",
        "direction": "Both directions",
        "toll_points": [{"name": "Cross City Tunnel", "lat": -33.8750, "lng": 151.2090}],
        "prices": {
            "car": {"peak": 6.72, "off_peak": 6.72, "weekend": 6.72},
            "motorcycle": {"peak": 2.49, "off_peak": 2.49, "weekend": 2.49},
            "heavy": {"peak": 13.44, "off_peak": 13.44, "weekend": 13.44},
        },
        "notes": "Flat rate — no peak/off-peak difference.",
    },
    {
        "name": "Lane Cove Tunnel",
        "operator": "Transurban",
        "direction": "Both directions",
        "toll_points": [{"name": "Lane Cove Tunnel", "lat": -33.8170, "lng": 151.1580}],
        "prices": {
            "car": {"peak": 4.07, "off_peak": 2.77, "weekend": 2.77},
            "motorcycle": {"peak": 1.51, "off_peak": 1.03, "weekend": 1.03},
            "heavy": {"peak": 8.14, "off_peak": 5.54, "weekend": 5.54},
        },
        "notes": "Peak: Mon-Fri 6:30-9:30am, 4-7pm.",
    },
    {
        "name": "NorthConnex",
        "operator": "Transurban",
        "direction": "Both directions",
        "toll_points": [
            {"name": "NorthConnex south", "lat": -33.7640, "lng": 151.0680},
            {"name": "NorthConnex north", "lat": -33.7200, "lng": 151.1180},
        ],
        "prices": {
            "car": {"peak": 8.95, "off_peak": 6.08, "weekend": 6.08},
            "motorcycle": {"peak": 3.31, "off_peak": 2.25, "weekend": 2.25},
            "heavy": {"peak": 26.85, "off_peak": 18.24, "weekend": 18.24},
        },
        "notes": "Heavy vehicles MUST use NorthConnex (banned from Pennant Hills Rd). Peak: Mon-Fri 6:30-9:30am, 4-7pm.",
    },
    {
        "name": "M6 Motorway (Stage 1)",
        "operator": "Transurban (WestConnex)",
        "direction": "Both directions",
        "toll_points": [{"name": "M6 Arncliffe", "lat": -33.9370, "lng": 151.1540}],
        "prices": {
            "car": {"peak": 3.91, "off_peak": 2.66, "weekend": 2.66},
            "motorcycle": {"peak": 1.45, "off_peak": 0.98, "weekend": 0.98},
            "heavy": {"peak": 7.82, "off_peak": 5.32, "weekend": 5.32},
        },
        "notes": "Opened 2025. Connects M8 at Arncliffe to Kogarah/President Avenue.",
    },
]

# ---------------------------------------------------------------------------
# Registration info (included in all responses)
# ---------------------------------------------------------------------------

REGISTRATION_INFO = {
    "message": "Sydney toll roads use electronic tolling (E-TAG or license plate matching)",
    "providers": ["Linkt (linkt.com.au)", "E-Toll (myetoll.com.au)"],
    "tip": "Get a tag to avoid the extra license plate matching fee (~$0.55 per trip)",
}

SOURCE = "NSW Government / Toll road operators (prices as of March 2026)"

# ---------------------------------------------------------------------------
# Time period detection
# ---------------------------------------------------------------------------


def detect_time_period() -> str:
    """Auto-detect peak/off_peak/weekend based on current day and time."""
    now = datetime.now()
    weekday = now.weekday()  # 0=Mon, 6=Sun
    hour = now.hour
    minute = now.minute
    time_minutes = hour * 60 + minute

    # Weekend: Saturday (5) or Sunday (6)
    if weekday >= 5:
        return "weekend"

    # Peak: Mon-Fri 6:30-9:30am (390-570) or 4-7pm (960-1140)
    morning_peak_start = 6 * 60 + 30   # 6:30am = 390
    morning_peak_end = 9 * 60 + 30     # 9:30am = 570
    evening_peak_start = 16 * 60       # 4:00pm = 960
    evening_peak_end = 19 * 60         # 7:00pm = 1140

    if morning_peak_start <= time_minutes < morning_peak_end:
        return "peak"
    if evening_peak_start <= time_minutes < evening_peak_end:
        return "peak"

    return "off_peak"


def normalize_time_period(raw: str) -> str:
    """Normalize user input to internal time period key."""
    raw = raw.lower().strip().replace("-", "_").replace(" ", "_")
    aliases = {
        "peak": "peak",
        "offpeak": "off_peak",
        "off_peak": "off_peak",
        "weekend": "weekend",
        "sat": "weekend",
        "sun": "weekend",
        "saturday": "weekend",
        "sunday": "weekend",
    }
    return aliases.get(raw, "off_peak")


# ---------------------------------------------------------------------------
# Fuzzy road name matching
# ---------------------------------------------------------------------------


def fuzzy_match_road(query: str) -> list[dict]:
    """Fuzzy match a toll road by name. Returns matching roads sorted by relevance."""
    query_lower = query.lower().strip()
    exact = []
    contains = []
    partial = []

    for road in TOLL_ROADS:
        name_lower = road["name"].lower()
        if query_lower == name_lower:
            exact.append(road)
        elif query_lower in name_lower:
            contains.append(road)
        else:
            # Check individual words
            query_words = query_lower.split()
            if all(w in name_lower for w in query_words):
                partial.append(road)

    return exact + contains + partial


# ---------------------------------------------------------------------------
# Route toll calculation
# ---------------------------------------------------------------------------


def point_to_line_distance_km(
    point_lat: float, point_lng: float,
    line_lat1: float, line_lng1: float,
    line_lat2: float, line_lng2: float,
) -> float:
    """
    Approximate distance from a point to the nearest point on a line segment
    defined by two endpoints. Uses simple projection in lat/lng space then
    haversine for the final distance.
    """
    # Vector from line start to line end
    dx = line_lat2 - line_lat1
    dy = line_lng2 - line_lng1
    len_sq = dx * dx + dy * dy

    if len_sq == 0:
        # Line segment is a point
        return haversine_km(point_lat, point_lng, line_lat1, line_lng1)

    # Project point onto line, clamped to [0, 1]
    t = max(0, min(1, (
        (point_lat - line_lat1) * dx + (point_lng - line_lng1) * dy
    ) / len_sq))

    proj_lat = line_lat1 + t * dx
    proj_lng = line_lng1 + t * dy

    return haversine_km(point_lat, point_lng, proj_lat, proj_lng)


def find_toll_roads_on_route(
    from_loc: Location, to_loc: Location, threshold_km: float = 5.0
) -> list[dict]:
    """
    Find toll roads whose toll points are within threshold_km of the straight
    line between from_loc and to_loc. This is an approximation — actual route
    may differ from straight line.
    """
    matched = []

    for road in TOLL_ROADS:
        for tp in road["toll_points"]:
            dist = point_to_line_distance_km(
                tp["lat"], tp["lng"],
                from_loc.lat, from_loc.lng,
                to_loc.lat, to_loc.lng,
            )
            if dist <= threshold_km:
                matched.append(road)
                break  # Don't double-count the same road

    return matched


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_road(road: dict, vehicle: str, time_period: str) -> dict:
    """Format a single toll road for JSON output."""
    price = road["prices"].get(vehicle, road["prices"]["car"]).get(time_period, 0)
    return {
        "name": road["name"],
        "operator": road["operator"],
        "direction": road["direction"],
        "price": price,
        "vehicle": vehicle,
        "time_period": time_period,
        "notes": road["notes"],
        "toll_points": road["toll_points"],
    }


def build_all_roads_output(vehicle: str, time_period: str) -> dict:
    """Build JSON output listing all toll roads."""
    roads = [format_road(r, vehicle, time_period) for r in TOLL_ROADS]
    return {
        "query": {"mode": "all", "vehicle": vehicle, "time": time_period},
        "results": {
            "count": len(roads),
            "toll_roads": roads,
        },
        "registration_info": REGISTRATION_INFO,
        "source": SOURCE,
    }


def build_road_output(query: str, matches: list[dict], vehicle: str, time_period: str) -> dict:
    """Build JSON output for a specific road search."""
    roads = [format_road(r, vehicle, time_period) for r in matches]
    return {
        "query": {"road": query, "vehicle": vehicle, "time": time_period},
        "results": {
            "count": len(roads),
            "toll_roads": roads,
        },
        "registration_info": REGISTRATION_INFO,
        "source": SOURCE,
    }


def build_route_output(
    from_name: str, to_name: str,
    from_loc: Location, to_loc: Location,
    matched_roads: list[dict],
    vehicle: str, time_period: str,
) -> dict:
    """Build JSON output for a route toll calculation."""
    roads = [format_road(r, vehicle, time_period) for r in matched_roads]
    total = sum(r["price"] for r in roads)
    return {
        "query": {"from": from_name, "to": to_name, "vehicle": vehicle, "time": time_period},
        "route": {
            "from": {"name": from_name, "lat": from_loc.lat, "lng": from_loc.lng},
            "to": {"name": to_name, "lat": to_loc.lat, "lng": to_loc.lng},
        },
        "results": {
            "toll_roads_on_route": roads,
            "total_toll": round(total, 2),
            "vehicle": vehicle,
            "time_period": time_period,
        },
        "registration_info": REGISTRATION_INFO,
        "source": SOURCE,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sydney toll road prices and route toll calculator."
    )
    parser.add_argument(
        "--road",
        type=str,
        default=None,
        help="Search for a specific toll road by name (fuzzy match).",
    )
    parser.add_argument(
        "--from",
        dest="from_place",
        type=str,
        default=None,
        help="Origin location for route toll calculation.",
    )
    parser.add_argument(
        "--to",
        dest="to_place",
        type=str,
        default=None,
        help="Destination location for route toll calculation.",
    )
    parser.add_argument(
        "--vehicle",
        type=str,
        choices=["car", "motorcycle", "heavy"],
        default="car",
        help="Vehicle type (default: car).",
    )
    parser.add_argument(
        "--time",
        type=str,
        default=None,
        help="Time period: peak, offpeak, weekend (default: auto-detect).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="List all toll roads with current prices.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        default=False,
        help="Skip cache and force fresh geocoding.",
    )
    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    import httpx

    parser = build_parser()
    args = parser.parse_args()

    # Determine time period
    if args.time:
        time_period = normalize_time_period(args.time)
    else:
        time_period = detect_time_period()

    vehicle = args.vehicle

    print(f"vehicle={vehicle} time={time_period}", file=sys.stderr)

    # --- Mode: specific road ---
    if args.road:
        print(f"Searching for toll road: {args.road}", file=sys.stderr)
        matches = fuzzy_match_road(args.road)
        if not matches:
            output = {
                "query": {"road": args.road, "vehicle": vehicle, "time": time_period},
                "error": f"No toll road found matching '{args.road}'",
                "available_roads": [r["name"] for r in TOLL_ROADS],
                "registration_info": REGISTRATION_INFO,
                "source": SOURCE,
            }
        else:
            output = build_road_output(args.road, matches, vehicle, time_period)
        json.dump(output, sys.stdout, indent=2)
        print(file=sys.stdout)
        return

    # --- Mode: route calculation ---
    if args.from_place and args.to_place:
        print(f"Calculating route tolls: {args.from_place} -> {args.to_place}", file=sys.stderr)

        async with httpx.AsyncClient() as client:
            # Check cache for geocoded locations
            from_cache_key = f"geo_{args.from_place.lower().replace(' ', '_')}"
            to_cache_key = f"geo_{args.to_place.lower().replace(' ', '_')}"

            from_loc = None
            to_loc = None

            if not args.no_cache:
                cached_from = cache_get(from_cache_key)
                if cached_from:
                    from_loc = Location(**cached_from)
                    print(f"  from (cached): {from_loc.city}, {from_loc.state}", file=sys.stderr)

                cached_to = cache_get(to_cache_key)
                if cached_to:
                    to_loc = Location(**cached_to)
                    print(f"  to (cached): {to_loc.city}, {to_loc.state}", file=sys.stderr)

            if not from_loc:
                from_loc = await _geocode_forward(client, args.from_place)
                if from_loc:
                    cache_set(from_cache_key, {
                        "lat": from_loc.lat, "lng": from_loc.lng,
                        "city": from_loc.city, "state": from_loc.state,
                        "postcode": from_loc.postcode, "country": from_loc.country,
                        "method": from_loc.method,
                    })
                    print(f"  from (geocoded): {from_loc.city}, {from_loc.state} ({from_loc.lat}, {from_loc.lng})", file=sys.stderr)

            if not to_loc:
                to_loc = await _geocode_forward(client, args.to_place)
                if to_loc:
                    cache_set(to_cache_key, {
                        "lat": to_loc.lat, "lng": to_loc.lng,
                        "city": to_loc.city, "state": to_loc.state,
                        "postcode": to_loc.postcode, "country": to_loc.country,
                        "method": to_loc.method,
                    })
                    print(f"  to (geocoded): {to_loc.city}, {to_loc.state} ({to_loc.lat}, {to_loc.lng})", file=sys.stderr)

        if not from_loc:
            output = {
                "query": {"from": args.from_place, "to": args.to_place, "vehicle": vehicle, "time": time_period},
                "error": f"Could not geocode origin: '{args.from_place}'",
                "registration_info": REGISTRATION_INFO,
                "source": SOURCE,
            }
            json.dump(output, sys.stdout, indent=2)
            print(file=sys.stdout)
            return

        if not to_loc:
            output = {
                "query": {"from": args.from_place, "to": args.to_place, "vehicle": vehicle, "time": time_period},
                "error": f"Could not geocode destination: '{args.to_place}'",
                "registration_info": REGISTRATION_INFO,
                "source": SOURCE,
            }
            json.dump(output, sys.stdout, indent=2)
            print(file=sys.stdout)
            return

        matched_roads = find_toll_roads_on_route(from_loc, to_loc)
        print(f"  toll roads on route: {len(matched_roads)}", file=sys.stderr)

        output = build_route_output(
            args.from_place, args.to_place,
            from_loc, to_loc,
            matched_roads, vehicle, time_period,
        )
        json.dump(output, sys.stdout, indent=2)
        print(file=sys.stdout)
        return

    # --- Mode: from/to incomplete ---
    if args.from_place or args.to_place:
        output = {
            "error": "Both --from and --to are required for route toll calculation.",
            "registration_info": REGISTRATION_INFO,
            "source": SOURCE,
        }
        json.dump(output, sys.stdout, indent=2)
        print(file=sys.stdout)
        return

    # --- Mode: list all (default) ---
    print("Listing all Sydney toll roads", file=sys.stderr)
    output = build_all_roads_output(vehicle, time_period)
    json.dump(output, sys.stdout, indent=2)
    print(file=sys.stdout)


if __name__ == "__main__":
    asyncio.run(main())
