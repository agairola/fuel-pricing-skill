#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27.0",
# ]
# ///
"""
Beach Check NSW — check water quality and swimming safety at NSW beaches.

Zero-config: works immediately with no API keys.

Usage:
    uv run beach_check.py                              # nearby beaches (auto-detect location)
    uv run beach_check.py --beach "Bondi"              # search by beach name
    uv run beach_check.py --location "Coogee NSW"      # nearby beaches by suburb
    uv run beach_check.py --lat -33.92 --lng 151.26    # nearby beaches by coordinates
    uv run beach_check.py --radius 20                  # wider search radius
"""

import argparse
import asyncio
import json
import math
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote, quote_plus

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

WATER_QUALITY_RATINGS = {
    1: "Bad",
    2: "Poor",
    3: "Fair",
    4: "Good",
}

STALENESS_THRESHOLD_DAYS = 7


@dataclass
class Location:
    lat: float
    lng: float
    city: str
    state: str
    postcode: str
    country: str
    method: str  # how we detected it


@dataclass
class Beach:
    name: str
    id: str
    water_quality: str
    water_quality_rating: int
    pollution_forecast: str
    pollution_forecast_time: str
    observation_date: str
    lat: float
    lng: float
    distance_km: float | None = None
    stale: bool = False
    stale_note: str = ""


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

CACHE_DIR = Path.home() / ".config" / "beach-check"
CACHE_TTL_SECONDS = 3600  # 1 hour


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
# Geolocation
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

NOMINATIM_HEADERS = {"User-Agent": "beach-check-cli/1.0"}

LOCATION_HTML = """<!DOCTYPE html>
<html><head><title>Beach Check - Location</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         display: flex; justify-content: center; align-items: center; height: 100vh;
         margin: 0; background: #f5f5f5; }
  .card { background: white; border-radius: 12px; padding: 40px; text-align: center;
          box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 400px; }
  h2 { margin: 0 0 8px; }
  p { color: #666; margin: 0 0 24px; }
  .status { font-size: 18px; color: #333; }
  .ok { color: #22863a; }
  .err { color: #cb2431; }
</style></head>
<body><div class="card">
  <h2>Beach Check</h2>
  <p>Allow location access to find beach conditions near you.</p>
  <div class="status" id="s">Requesting location...</div>
</div>
<script>
if (!navigator.geolocation) {
  document.getElementById('s').innerHTML = '<span class="err">Geolocation not supported</span>';
} else {
  navigator.geolocation.getCurrentPosition(
    function(pos) {
      document.getElementById('s').innerHTML = '<span class="ok">Location found! You can close this tab.</span>';
      fetch('/callback', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({lat: pos.coords.latitude, lng: pos.coords.longitude,
                              accuracy: pos.coords.accuracy})
      });
    },
    function(err) {
      document.getElementById('s').innerHTML = '<span class="err">Location denied: ' + err.message + '</span>';
      fetch('/callback', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({error: err.message})});
    },
    {enableHighAccuracy: true, timeout: 15000, maximumAge: 60000}
  );
}
</script></body></html>"""


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


async def _geocode_reverse(
    client: "httpx.AsyncClient", lat: float, lng: float
) -> dict | None:
    """Reverse geocode via Nominatim /reverse — convert coords to address info."""
    try:
        resp = await client.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "lat": lat,
                "lon": lng,
                "format": "jsonv2",
                "addressdetails": 1,
            },
            headers=NOMINATIM_HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        addr = data.get("address", {})
        suburb = addr.get("suburb") or addr.get("town") or addr.get("city") or ""
        state_raw = addr.get("state", "")
        state = AU_STATES.get(state_raw, state_raw)
        return {
            "city": suburb,
            "state": state,
            "postcode": addr.get("postcode", ""),
            "country": addr.get("country", "Australia"),
        }
    except Exception:
        return None


async def _geolocate_browser() -> Location | None:
    """Browser-based geolocation — opens localhost page that requests navigator.geolocation.

    Same pattern as `gh auth login` / `gcloud auth login`: spawn local server, open browser,
    get data back via callback. Works on all OSes, uses WiFi triangulation via the browser
    (~15-50 foot accuracy). The user sees the standard browser location prompt they're
    familiar with from websites.
    """
    import http.server
    import threading
    import webbrowser

    result_holder: dict = {}
    server_ready = threading.Event()
    got_result = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(LOCATION_HTML.encode())

        def do_POST(self):
            if self.path == "/callback":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    result_holder.update(json.loads(body))
                except json.JSONDecodeError:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")
                got_result.set()

        def log_message(self, format, *args):
            pass  # Suppress server logs

    # Find a free port
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1  # 1-second poll so we can check got_result

    def run_server():
        server_ready.set()
        deadline = time.time() + 30
        while not got_result.is_set() and time.time() < deadline:
            server.handle_request()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    server_ready.wait()

    url = f"http://127.0.0.1:{port}"
    print("Opening browser for location access...", file=sys.stderr)
    webbrowser.open(url)

    # Wait for the callback (timeout 30 seconds)
    got_result.wait(timeout=30)
    server.server_close()

    if "error" in result_holder or "lat" not in result_holder:
        return None

    return Location(
        lat=result_holder["lat"],
        lng=result_holder["lng"],
        city="",
        state="",
        postcode="",
        country="",
        method=f"browser (accuracy: {result_holder.get('accuracy', '?')}m)",
    )


async def _geolocate_ip(client: "httpx.AsyncClient") -> Location | None:
    """IP-based geolocation via ip-api.com — city-level, no key needed."""
    try:
        resp = await client.get(
            "http://ip-api.com/json/",
            params={"fields": "status,country,regionName,city,zip,lat,lon,timezone"},
            timeout=5,
        )
        data = resp.json()
        if data.get("status") != "success":
            return None
        state_raw = data.get("regionName", "")
        state = AU_STATES.get(state_raw, state_raw)
        return Location(
            lat=data["lat"],
            lng=data["lon"],
            city=data.get("city", ""),
            state=state,
            postcode=data.get("zip", ""),
            country=data.get("country", ""),
            method="ip-api.com",
        )
    except Exception:
        return None


def _get_cached_location() -> Location | None:
    """Read cached location from disk. Expires after 24 hours."""
    path = CACHE_DIR / "location.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        # Expire after 24 hours
        if time.time() - data.get("_cached_at", 0) > 86400:
            return None
        return Location(**data["location"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _cache_location(loc: Location) -> None:
    """Cache location to disk for 24 hours."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / "location.json"
    path.write_text(json.dumps({
        "_cached_at": time.time(),
        "location": {
            "lat": loc.lat, "lng": loc.lng, "city": loc.city,
            "state": loc.state, "postcode": loc.postcode,
            "country": loc.country, "method": loc.method,
        },
    }))


async def geolocate(client: "httpx.AsyncClient") -> Location | None:
    """Try the best available geolocation method.

    Chain:
      1. Cached location (from previous browser consent)
      2. Browser consent flow (opens browser, ~15-50ft accuracy, all OSes)
      3. IP geolocation (fallback, city-level accuracy)
    """
    # 1. Check for cached location (from a previous browser consent)
    cached_loc = _get_cached_location()
    if cached_loc:
        return cached_loc

    # 2. Try browser-based geolocation (works on all OSes, most accurate)
    browser_loc = await _geolocate_browser()
    if browser_loc:
        # Enrich with city/state/postcode via reverse geocoding (accurate to suburb)
        rev = await _geocode_reverse(client, browser_loc.lat, browser_loc.lng)
        if rev:
            browser_loc.city = rev["city"]
            browser_loc.state = rev["state"]
            browser_loc.postcode = rev["postcode"]
            browser_loc.country = rev["country"]
        else:
            # Fall back to IP enrichment if Nominatim fails
            ip_loc = await _geolocate_ip(client)
            if ip_loc:
                browser_loc.state = ip_loc.state
                browser_loc.postcode = ip_loc.postcode
                browser_loc.city = ip_loc.city
                browser_loc.country = ip_loc.country
        # Cache for future runs so the browser doesn't open every time
        _cache_location(browser_loc)
        return browser_loc

    # 3. Fallback to IP geolocation (works everywhere, city-level accuracy)
    return await _geolocate_ip(client)


def location_from_args(
    args: argparse.Namespace, client: "httpx.AsyncClient"
) -> "asyncio.coroutine":
    """Build a Location from CLI args, or auto-detect."""

    async def _resolve() -> Location | None:
        if args.lat is not None and args.lng is not None:
            # Reverse geocode to get accurate suburb/state/postcode
            rev = await _geocode_reverse(client, args.lat, args.lng)
            if rev:
                return Location(
                    lat=args.lat,
                    lng=args.lng,
                    city=args.location or rev["city"],
                    state=rev["state"],
                    postcode=rev["postcode"],
                    country=rev["country"],
                    method="manual",
                )
            # Fall back to IP enrichment if Nominatim fails
            ip_loc = await _geolocate_ip(client)
            return Location(
                lat=args.lat,
                lng=args.lng,
                city=args.location or (ip_loc.city if ip_loc else ""),
                state=ip_loc.state if ip_loc else "",
                postcode=ip_loc.postcode if ip_loc else "",
                country="AU",
                method="manual",
            )
        if args.location:
            # Forward geocode to get accurate coords for the place
            geo_loc = await _geocode_forward(client, args.location)
            if geo_loc:
                return geo_loc
            # Fall back to IP-based behavior if Nominatim fails
            print(f"Warning: geocoding failed for '{args.location}', falling back to IP geolocation", file=sys.stderr)
            ip_loc = await _geolocate_ip(client)
            if ip_loc:
                ip_loc.city = args.location or ip_loc.city
                ip_loc.method = "ip-fallback"
                return ip_loc
            # Can't geolocate at all — return a stub
            return Location(
                lat=0,
                lng=0,
                city=args.location or "",
                state="",
                postcode="",
                country="AU",
                method="manual",
            )
        # Auto-detect
        return await geolocate(client)

    return _resolve()


# ---------------------------------------------------------------------------
# Distance calculation
# ---------------------------------------------------------------------------


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
# Beach name matching
# ---------------------------------------------------------------------------


def _normalize(s: str) -> str:
    """Lowercase and strip extra whitespace."""
    return " ".join(s.lower().split())


def _similarity_score(query: str, name: str) -> float:
    """Compute a simple similarity score between query and beach name.

    Returns a float between 0 and 1. Higher is better.
    """
    q = _normalize(query)
    n = _normalize(name)

    # Exact match
    if q == n:
        return 1.0

    # Substring match
    if q in n:
        return 0.9

    # Starts-with match
    if n.startswith(q):
        return 0.85

    # Word overlap
    q_words = set(q.split())
    n_words = set(n.split())
    if not q_words:
        return 0.0
    common = q_words & n_words
    if common:
        return 0.5 + 0.3 * (len(common) / max(len(q_words), len(n_words)))

    # Prefix match on individual words
    for qw in q_words:
        for nw in n_words:
            if nw.startswith(qw) or qw.startswith(nw):
                return 0.4

    return 0.0


def match_beaches(query: str, features: list[dict]) -> tuple[list[dict], list[dict]]:
    """Match a beach name query against GeoJSON features.

    Returns (matches, alternatives) where matches are strong hits
    and alternatives are close but weaker matches.
    """
    scored = []
    for f in features:
        name = f.get("properties", {}).get("siteName", "")
        score = _similarity_score(query, name)
        if score > 0:
            scored.append((score, f))

    # Sort by score descending, then by name length ascending (prefer shorter/more specific names)
    scored.sort(key=lambda x: (-x[0], len(x[1].get("properties", {}).get("siteName", ""))))

    if not scored:
        return [], []

    best_score = scored[0][0]
    # Strong matches: within 0.1 of best score
    matches = [f for s, f in scored if s >= best_score - 0.05][:1]
    # Alternatives: next best that aren't in matches
    match_ids = {f["properties"]["id"] for f in matches}
    alternatives = [
        f for s, f in scored if f["properties"]["id"] not in match_ids and s > 0.2
    ][:3]

    return matches, alternatives


# ---------------------------------------------------------------------------
# Map URLs
# ---------------------------------------------------------------------------


def _google_maps_url(name: str) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(name)}"


def _apple_maps_url(name: str, lat: float, lng: float) -> str:
    return f"https://maps.apple.com/?q={quote_plus(name)}&ll={lat},{lng}"


# ---------------------------------------------------------------------------
# Beach data formatting
# ---------------------------------------------------------------------------


def _feature_to_beach(feature: dict, user_lat: float | None = None, user_lng: float | None = None) -> dict:
    """Convert a GeoJSON feature to a beach result dict."""
    props = feature.get("properties", {})
    coords = feature.get("geometry", {}).get("coordinates", [0, 0])
    # GeoJSON is [lng, lat]
    lng = coords[0]
    lat = coords[1]

    name = props.get("siteName", "Unknown")
    observation_date = props.get("latestResultObservationDate", "")
    pollution_forecast_time = props.get("pollutionForecastTimeStamp", "")

    beach = {
        "name": name,
        "id": props.get("id", ""),
        "water_quality": props.get("latestResult", "Unknown"),
        "water_quality_rating": props.get("latestResultRating", 0),
        "pollution_forecast": props.get("pollutionForecast", "Forecast not available"),
        "pollution_forecast_time": pollution_forecast_time,
        "observation_date": observation_date,
        "lat": lat,
        "lng": lng,
        "google_maps_url": _google_maps_url(name),
        "apple_maps_url": _apple_maps_url(name, lat, lng),
    }

    # Distance from user
    if user_lat is not None and user_lng is not None:
        beach["distance_km"] = round(haversine_km(user_lat, user_lng, lat, lng), 1)

    # Staleness detection
    if observation_date:
        try:
            from datetime import datetime, timezone

            obs_dt = datetime.fromisoformat(observation_date.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - obs_dt).days
            if age_days > STALENESS_THRESHOLD_DAYS:
                beach["stale"] = True
                beach["stale_note"] = (
                    f"Water quality data is {age_days} days old and may not reflect current conditions."
                )
        except (ValueError, TypeError):
            pass

    return beach


def _feature_to_alternative(feature: dict, user_lat: float | None = None, user_lng: float | None = None) -> dict:
    """Convert a GeoJSON feature to a compact alternative dict."""
    props = feature.get("properties", {})
    coords = feature.get("geometry", {}).get("coordinates", [0, 0])
    lng = coords[0]
    lat = coords[1]

    alt = {
        "name": props.get("siteName", "Unknown"),
        "water_quality": props.get("latestResult", "Unknown"),
    }

    if user_lat is not None and user_lng is not None:
        alt["distance_km"] = round(haversine_km(user_lat, user_lng, lat, lng), 1)

    return alt


# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------


BEACHWATCH_URL = "https://api.beachwatch.nsw.gov.au/public/sites/geojson"


async def fetch_beach_data(client: "httpx.AsyncClient") -> dict | None:
    """Fetch all beach data from the Beachwatch API."""
    try:
        resp = await client.get(BEACHWATCH_URL, timeout=15)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as e:
        print(f"Warning: Beachwatch API request failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check beach water quality at NSW beaches")
    parser.add_argument("--beach", "-b", help="Beach name to search for (fuzzy match)")
    parser.add_argument("--location", "-l", help="Suburb or city name (e.g., 'Coogee NSW')")
    parser.add_argument("--lat", type=float, help="Latitude")
    parser.add_argument("--lng", type=float, help="Longitude")
    parser.add_argument(
        "--radius", "-r", type=float, default=10.0, help="Search radius in km (default: 10)"
    )
    parser.add_argument("--no-cache", action="store_true", help="Skip cache")
    return parser.parse_args()


async def main() -> None:
    import httpx

    args = parse_args()

    # Cache key
    cache_key = f"beach_data"
    if args.no_cache:
        # Clear both location and beach data cache
        loc_cache = CACHE_DIR / "location.json"
        if loc_cache.exists():
            loc_cache.unlink()
        data_cache = _cache_path(cache_key)
        if data_cache.exists():
            data_cache.unlink()

    # Fetch beach data (single GET, cached 1hr)
    cached_data = None if args.no_cache else cache_get(cache_key)
    if cached_data:
        geojson = cached_data
    else:
        async with httpx.AsyncClient() as client:
            geojson = await fetch_beach_data(client)
        if not geojson:
            print(json.dumps({"error": "Failed to fetch beach data from Beachwatch API."}))
            sys.exit(1)
        cache_set(cache_key, geojson)

    features = geojson.get("features", [])
    if not features:
        print(json.dumps({"error": "No beach data available from Beachwatch API."}))
        sys.exit(1)

    # --beach mode: search by name
    if args.beach:
        matches, alternatives = match_beaches(args.beach, features)

        if not matches:
            print(json.dumps({
                "error": f"No beaches found matching '{args.beach}'.",
                "query": {"beach": args.beach, "mode": "beach_search"},
            }))
            sys.exit(1)

        # Try to resolve location for context (non-critical)
        location_info = {"city": "", "state": "NSW", "postcode": "", "lat": 0, "lng": 0, "method": "beach_search", "confidence": "high"}

        # Use the matched beach's coordinates as reference
        match_feature = matches[0]
        match_coords = match_feature.get("geometry", {}).get("coordinates", [0, 0])
        match_lat = match_coords[1]
        match_lng = match_coords[0]

        beach_results = [_feature_to_beach(f) for f in matches]
        alt_results = [_feature_to_alternative(f, match_lat, match_lng) for f in alternatives]

        result = {
            "location": location_info,
            "query": {"beach": args.beach, "mode": "beach_search"},
            "results": {
                "count": len(beach_results),
                "beaches": beach_results,
                "alternatives": alt_results,
            },
        }

        print(json.dumps(result, indent=2, default=str))
        return

    # Nearby mode: resolve location first
    async with httpx.AsyncClient() as client:
        location = await location_from_args(args, client)

    if not location:
        print(json.dumps({"error": "Could not determine location. Use --location or --lat/--lng."}))
        sys.exit(1)

    if location.country and location.country not in ("Australia", "AU"):
        print(json.dumps({
            "error": f"Detected location in {location.country}. This tool covers NSW beaches only.",
            "detected": {
                "city": location.city,
                "country": location.country,
                "lat": location.lat,
                "lng": location.lng,
            },
        }))
        sys.exit(1)

    # Flag IP-only detection so the agent knows accuracy is limited
    if location.method in ("ip-api.com", "ip-fallback") and not (args.lat and args.lng):
        location_confidence = "low"
    else:
        location_confidence = "high"

    # Filter beaches within radius
    nearby = []
    for f in features:
        coords = f.get("geometry", {}).get("coordinates", [0, 0])
        blng = coords[0]
        blat = coords[1]
        dist = haversine_km(location.lat, location.lng, blat, blng)
        if dist <= args.radius:
            nearby.append((dist, f))

    nearby.sort(key=lambda x: x[0])
    nearby = nearby[:10]  # Top 10

    beach_results = []
    for dist, f in nearby:
        beach = _feature_to_beach(f, location.lat, location.lng)
        beach_results.append(beach)

    result = {
        "location": {
            "city": location.city,
            "state": location.state,
            "postcode": location.postcode,
            "lat": location.lat,
            "lng": location.lng,
            "method": location.method,
            "confidence": location_confidence,
        },
        "query": {"radius_km": args.radius, "mode": "nearby"},
        "results": {
            "count": len(beach_results),
            "beaches": beach_results,
        },
    }

    if location_confidence == "low":
        result["location"]["note"] = (
            "Location was detected via IP address only (city-level accuracy). "
            "The user may not actually be in this area. Ask them to confirm their "
            "suburb or postcode for accurate results."
        )

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
