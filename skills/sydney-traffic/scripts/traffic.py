#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27.0",
# ]
# ///
"""
Sydney Traffic — check live traffic incidents, roadworks, and hazards in Sydney.

Zero-config: works immediately with no API keys (provides Live Traffic NSW / Google Maps links).
Optional: save TfNSW API key to ~/.config/sydney-commute/credentials.json for real-time data.

Usage:
    uv run traffic.py                                    # nearby incidents (auto-detect location)
    uv run traffic.py --location "Parramatta NSW"        # incidents near a suburb
    uv run traffic.py --lat -33.87 --lng 151.21          # incidents near coordinates
    uv run traffic.py --radius 20                        # wider search radius
    uv run traffic.py --type roadwork                    # only roadworks
    uv run traffic.py --road "M5"                        # filter by road name
"""

import argparse
import asyncio
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

HAZARD_TYPES = ("incident", "roadwork", "fire", "flood")

HAZARD_ENDPOINTS = {
    "incident": "https://api.transport.nsw.gov.au/v1/live/hazards/incident/open",
    "roadwork": "https://api.transport.nsw.gov.au/v1/live/hazards/roadwork/open",
    "fire": "https://api.transport.nsw.gov.au/v1/live/hazards/fire/open",
    "flood": "https://api.transport.nsw.gov.au/v1/live/hazards/flood/open",
}


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

CACHE_DIR = Path.home() / ".config" / "sydney-traffic"
CACHE_TTL_SECONDS = 300  # 5 minutes

# Credentials are shared with sydney-commute
CREDENTIALS_PATH = Path.home() / ".config" / "sydney-commute" / "credentials.json"


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
# Credentials (secure file-based storage)
# ---------------------------------------------------------------------------


def _get_credentials() -> dict:
    """Read API credentials. File takes priority over env vars."""
    creds = {}
    # 1. Try credentials file (preferred -- chmod 600, not in shell env)
    if CREDENTIALS_PATH.exists():
        try:
            creds = json.loads(CREDENTIALS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    # 2. Fall back to env var
    if not creds.get("tfnsw_api_key"):
        key = os.environ.get("TFNSW_API_KEY", "")
        if key:
            creds["tfnsw_api_key"] = key
    return creds


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

NOMINATIM_HEADERS = {"User-Agent": "sydney-traffic-cli/1.0"}

LOCATION_HTML = """<!DOCTYPE html>
<html><head><title>Sydney Traffic - Location</title>
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
  <h2>Sydney Traffic</h2>
  <p>Allow location access to find traffic incidents near you.</p>
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
    """Forward geocode via Nominatim /search -- convert place name to coords."""
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
    """Reverse geocode via Nominatim /reverse -- convert coords to address info."""
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
    """Browser-based geolocation -- opens localhost page that requests navigator.geolocation.

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
    """IP-based geolocation via ip-api.com -- city-level, no key needed."""
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
            # Can't geolocate at all -- return a stub
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
# Road name matching
# ---------------------------------------------------------------------------


def _normalize(s: str) -> str:
    """Lowercase and strip extra whitespace."""
    return " ".join(s.lower().split())


def _road_matches(query: str, roads: list[str]) -> bool:
    """Fuzzy match a road name query against a list of road names."""
    q = _normalize(query)
    for road in roads:
        r = _normalize(road)
        # Exact match
        if q == r:
            return True
        # Substring match (e.g. "M5" matches "M5 East Motorway")
        if q in r:
            return True
        # Word overlap
        q_words = set(q.split())
        r_words = set(r.split())
        if q_words and q_words & r_words:
            return True
    return False


# ---------------------------------------------------------------------------
# Fallback URLs
# ---------------------------------------------------------------------------


def _build_fallback(location: Location) -> dict:
    """Build zero-config fallback response with Live Traffic NSW and Google Maps URLs."""
    google_maps_url = (
        f"https://www.google.com/maps/@{location.lat},{location.lng},13z/data=!5m1!1e1"
    )
    return {
        "location": {
            "city": location.city,
            "state": location.state,
            "lat": location.lat,
            "lng": location.lng,
            "method": location.method,
        },
        "api_key_configured": False,
        "fallback_urls": {
            "live_traffic": "https://www.livetraffic.com/",
            "google_maps_traffic": google_maps_url,
        },
        "upgrade": {
            "message": "For real-time traffic incidents, register for a free TfNSW API key (~2 minutes)",
            "steps": [
                "Sign up at opendata.transport.nsw.gov.au",
                "Create an application",
                "Subscribe to 'Traffic' APIs (free)",
                "Save key to ~/.config/sydney-commute/credentials.json",
            ],
            "url": "https://opendata.transport.nsw.gov.au",
        },
    }


# ---------------------------------------------------------------------------
# Hazard parsing
# ---------------------------------------------------------------------------


def _parse_hazard(feature: dict, hazard_type: str, user_lat: float, user_lng: float) -> dict | None:
    """Parse a single GeoJSON hazard feature into a result dict."""
    geometry = feature.get("geometry")
    if not geometry:
        return None

    coords = geometry.get("coordinates")
    if not coords or not isinstance(coords, list) or len(coords) < 2:
        return None

    # GeoJSON is [lng, lat]
    lng = coords[0]
    lat = coords[1]

    props = feature.get("properties", {})

    # Extract road names from the roads property
    roads_raw = props.get("roads", [])
    roads = []
    if isinstance(roads_raw, list):
        for r in roads_raw:
            if isinstance(r, dict):
                name = r.get("mainStreet") or r.get("crossStreet") or ""
                if name:
                    roads.append(name)
            elif isinstance(r, str):
                roads.append(r)
    elif isinstance(roads_raw, str):
        roads = [roads_raw]

    # Extract suburb from roads or properties
    suburb = props.get("suburb", "")
    if not suburb and isinstance(roads_raw, list):
        for r in roads_raw:
            if isinstance(r, dict) and r.get("suburb"):
                suburb = r["suburb"]
                break

    # Build advice from adviceA and adviceB
    advice_parts = []
    if props.get("adviceA"):
        advice_parts.append(props["adviceA"])
    if props.get("adviceB"):
        advice_parts.append(props["adviceB"])
    advice = " ".join(advice_parts) if advice_parts else ""

    distance = haversine_km(user_lat, user_lng, lat, lng)

    return {
        "type": hazard_type,
        "headline": props.get("headline", ""),
        "roads": roads,
        "suburb": suburb,
        "advice": advice,
        "lat": lat,
        "lng": lng,
        "distance_km": round(distance, 1),
        "last_updated": props.get("lastUpdated", props.get("created", "")),
        "link": "https://www.livetraffic.com/",
    }


# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------


async def fetch_hazards(
    client: "httpx.AsyncClient",
    api_key: str,
    hazard_type: str,
) -> list[dict]:
    """Fetch hazards of a given type from the TfNSW API."""
    url = HAZARD_ENDPOINTS.get(hazard_type)
    if not url:
        return []

    cache_key = f"hazards_{hazard_type}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        resp = await client.get(
            url,
            headers={"Authorization": f"apikey {api_key}"},
            timeout=15,
        )
        if resp.status_code == 401:
            print("Error: TfNSW API key is invalid (HTTP 401).", file=sys.stderr)
            return []
        if resp.status_code != 200:
            print(f"Warning: TfNSW API returned HTTP {resp.status_code} for {hazard_type}.", file=sys.stderr)
            return []
        data = resp.json()
        features = data.get("features", [])
        cache_set(cache_key, features)
        return features
    except Exception as e:
        print(f"Warning: TfNSW API request failed for {hazard_type}: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check live traffic incidents, roadworks, and hazards in Sydney"
    )
    parser.add_argument("--location", "-l", help="Suburb or city name (e.g., 'Parramatta NSW')")
    parser.add_argument("--lat", type=float, help="Latitude")
    parser.add_argument("--lng", type=float, help="Longitude")
    parser.add_argument(
        "--radius", "-r", type=float, default=10.0, help="Search radius in km (default: 10)"
    )
    parser.add_argument(
        "--type",
        "-t",
        choices=["incident", "roadwork", "fire", "flood", "all"],
        default="all",
        help="Hazard type filter (default: all)",
    )
    parser.add_argument("--road", help="Filter by road name (fuzzy match)")
    parser.add_argument("--no-cache", action="store_true", help="Skip cache")
    return parser.parse_args()


async def main() -> None:
    import httpx

    args = parse_args()

    # Clear caches if requested
    if args.no_cache:
        loc_cache = CACHE_DIR / "location.json"
        if loc_cache.exists():
            loc_cache.unlink()
        for ht in HAZARD_TYPES:
            cp = _cache_path(f"hazards_{ht}")
            if cp.exists():
                cp.unlink()

    async with httpx.AsyncClient() as client:
        # Resolve user location
        location = await location_from_args(args, client)

    if not location:
        print(json.dumps({"error": "Could not determine location. Use --location or --lat/--lng."}))
        sys.exit(1)

    # Check for TfNSW API key
    creds = _get_credentials()
    api_key = creds.get("tfnsw_api_key", "")

    if not api_key:
        # Zero-config fallback
        result = _build_fallback(location)
        print(json.dumps(result, indent=2, default=str))
        return

    # Determine which hazard types to fetch
    if args.type == "all":
        types_to_fetch = list(HAZARD_TYPES)
    else:
        types_to_fetch = [args.type]

    # Fetch hazards
    all_hazards = []
    async with httpx.AsyncClient() as client:
        for ht in types_to_fetch:
            features = await fetch_hazards(client, api_key, ht)
            for f in features:
                hazard = _parse_hazard(f, ht, location.lat, location.lng)
                if hazard is None:
                    continue
                # Filter by radius
                if hazard["distance_km"] > args.radius:
                    continue
                # Filter by road name if specified
                if args.road and not _road_matches(args.road, hazard["roads"]):
                    continue
                all_hazards.append(hazard)

    # Sort by distance
    all_hazards.sort(key=lambda h: h["distance_km"])

    # Flag IP-only detection so the agent knows accuracy is limited
    if location.method in ("ip-api.com", "ip-fallback") and not (args.lat and args.lng):
        location_confidence = "low"
    else:
        location_confidence = "high"

    location_info = {
        "city": location.city,
        "state": location.state,
        "lat": location.lat,
        "lng": location.lng,
        "method": location.method,
    }

    if location_confidence == "low":
        location_info["confidence"] = "low"
        location_info["note"] = (
            "Location was detected via IP address only (city-level accuracy). "
            "The user may not actually be in this area. Ask them to confirm their "
            "suburb or postcode for accurate results."
        )

    result = {
        "location": location_info,
        "query": {"radius_km": args.radius, "type": args.type},
        "api_key_configured": True,
        "results": {
            "count": len(all_hazards),
            "hazards": all_hazards,
        },
    }

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
