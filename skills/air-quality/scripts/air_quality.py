#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27.0",
# ]
# ///
"""
Air Quality NSW — check current air quality and pollution levels.

Zero-config: works immediately with no API keys.

Usage:
    uv run air_quality.py                          # auto-detect location
    uv run air_quality.py --location "Randwick"    # specify suburb
    uv run air_quality.py --site "RANDWICK"        # specify monitoring site
    uv run air_quality.py --pollutant PM2.5        # filter to one pollutant
    uv run air_quality.py --lat -33.8 --lng 151.2  # exact coordinates
"""

import argparse
import asyncio
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

ALL_POLLUTANTS = ["PM2.5", "PM10", "O3", "NO2", "CO", "NEPH"]

POLLUTANT_UNITS = {
    "PM2.5": "µg/m³",
    "PM10": "µg/m³",
    "O3": "pphm",
    "NO2": "pphm",
    "CO": "ppm",
    "NEPH": "10⁻⁴ m⁻¹",
}

HEALTH_ADVICE = {
    "Good": "Air quality is good. Enjoy outdoor activities.",
    "Fair": "Air quality is acceptable. Unusually sensitive people should consider reducing prolonged outdoor exertion.",
    "Poor": "Sensitive groups may experience health effects. Consider reducing prolonged outdoor exertion.",
    "Very Poor": "Health effects likely for everyone. Reduce prolonged outdoor exertion. Sensitive groups should avoid outdoor activity.",
    "Extremely Poor": "Health alert — everyone may experience serious health effects. Avoid outdoor activity.",
    "Hazardous": "Health emergency. Stay indoors with windows closed. Run air purifiers if available.",
}

EXERCISE_ADVICE = {
    "Good": "Safe to exercise outdoors.",
    "Fair": "Generally safe. Sensitive individuals may want to reduce intensity.",
    "Poor": "Consider indoor exercise. Sensitive groups should avoid outdoor exertion.",
    "Very Poor": "Exercise indoors only. Outdoor activity not recommended.",
    "Extremely Poor": "Do not exercise outdoors. Stay indoors.",
    "Hazardous": "Do not exercise. Minimise all physical exertion.",
}

# Category ordering from best to worst (for determining overall category)
CATEGORY_ORDER = ["Good", "Fair", "Poor", "Very Poor", "Extremely Poor", "Hazardous"]

API_BASE = "https://data.airquality.nsw.gov.au/api/Data"
MAX_SITE_DISTANCE_KM = 50.0
BUSHFIRE_PM25_THRESHOLD = 25.0
BUSHFIRE_PM25_HIGH = 50.0
BUSHFIRE_NEPH_THRESHOLD = 2.0


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

CACHE_DIR = Path.home() / ".config" / "air-quality"
SITE_CACHE_TTL = 86400   # 24 hours for site list
OBS_CACHE_TTL = 1800     # 30 minutes for observations


def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{key}.json"


def cache_get(key: str, ttl: int) -> dict | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if time.time() - data.get("_cached_at", 0) < ttl:
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


LOCATION_HTML = """<!DOCTYPE html>
<html><head><title>Air Quality - Location</title>
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
  <h2>Air Quality Check</h2>
  <p>Allow location access to find the nearest monitoring station.</p>
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
    print(f"Opening browser for location access...", file=sys.stderr)
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


NOMINATIM_HEADERS = {"User-Agent": "air-quality-cli/1.0"}


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
                state=(ip_loc.state if ip_loc else ""),
                postcode="",
                country="AU",
                method="manual",
            )
        if args.location:
            # Forward geocode to get accurate coords for the place
            query = args.location
            geo_loc = await _geocode_forward(client, query)
            if geo_loc:
                return geo_loc
            # Fall back to IP-based behavior if Nominatim fails
            print(f"Warning: geocoding failed for '{query}', falling back to IP geolocation", file=sys.stderr)
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
# Air Quality API
# ---------------------------------------------------------------------------


async def fetch_site_details(client: "httpx.AsyncClient", no_cache: bool = False) -> list[dict]:
    """GET /get_SiteDetails — returns all NSW monitoring sites."""
    if not no_cache:
        cached = cache_get("site_details", SITE_CACHE_TTL)
        if cached:
            print("Using cached site list", file=sys.stderr)
            return cached

    try:
        resp = await client.get(f"{API_BASE}/get_SiteDetails", timeout=15)
        resp.raise_for_status()
        sites = resp.json()
        cache_set("site_details", sites)
        return sites
    except Exception as e:
        print(f"Error fetching site details: {e}", file=sys.stderr)
        # Try cache even if expired
        cached = cache_get("site_details", SITE_CACHE_TTL * 7)
        if cached:
            print("Using stale cached site list", file=sys.stderr)
            return cached
        return []


def find_nearest_site(sites: list[dict], lat: float, lng: float) -> tuple[dict | None, float]:
    """Find the nearest monitoring site to the given coordinates."""
    best_site = None
    best_dist = float("inf")

    for site in sites:
        site_lat = site.get("Latitude")
        site_lng = site.get("Longitude")
        if site_lat is None or site_lng is None:
            continue
        dist = haversine_km(lat, lng, site_lat, site_lng)
        if dist < best_dist:
            best_dist = dist
            best_site = site

    return best_site, best_dist


def fuzzy_match_site(sites: list[dict], name: str) -> dict | None:
    """Fuzzy match a site name against the site list."""
    name_lower = name.lower().strip()

    # Exact match first
    for site in sites:
        if site.get("SiteName", "").lower() == name_lower:
            return site

    # Substring match
    matches = []
    for site in sites:
        site_name = site.get("SiteName", "").lower()
        if name_lower in site_name or site_name in name_lower:
            matches.append(site)

    if len(matches) == 1:
        return matches[0]

    # Partial word match
    if not matches:
        for site in sites:
            site_name = site.get("SiteName", "").lower()
            if any(word in site_name for word in name_lower.split()):
                matches.append(site)

    if len(matches) == 1:
        return matches[0]
    if matches:
        # Return the shortest name (most specific match)
        return min(matches, key=lambda s: len(s.get("SiteName", "")))

    return None


async def fetch_observations(
    client: "httpx.AsyncClient",
    site_id: int,
    pollutants: list[str],
    no_cache: bool = False,
) -> list[dict]:
    """POST /get_Observations — fetch hourly observations for a site.

    Requests yesterday+today to handle data lag (today's data may not be available yet).
    """
    today = date.today()
    yesterday = today - timedelta(days=1)
    cache_key = f"obs_{site_id}_{today.isoformat()}_{'_'.join(pollutants)}"

    if not no_cache:
        cached = cache_get(cache_key, OBS_CACHE_TTL)
        if cached:
            print("Using cached observations", file=sys.stderr)
            return cached

    body = {
        "Parameters": pollutants,
        "Sites": [site_id],
        "StartDate": yesterday.isoformat(),
        "EndDate": today.isoformat(),
        "Categories": ["Averages"],
        "SubCategories": ["Hourly"],
        "Frequency": ["Hourly average"],
    }

    try:
        resp = await client.post(
            f"{API_BASE}/get_Observations",
            json=body,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        values = data if isinstance(data, list) else data.get("Values", [])
        cache_set(cache_key, values)
        return values
    except Exception as e:
        print(f"Error fetching observations: {e}", file=sys.stderr)
        # Try cache even if expired
        cached = cache_get(cache_key, OBS_CACHE_TTL * 12)
        if cached:
            print("Using stale cached observations", file=sys.stderr)
            return cached
        return []


def extract_latest_readings(
    observations: list[dict], pollutant_filter: str | None = None
) -> tuple[list[dict], str, str]:
    """Extract the latest hour's readings from observations.

    Returns (readings, observation_time, observation_date).
    Groups by (date, hour) and tries from most recent backwards.
    """
    if not observations:
        return [], "", ""

    # Group by (date, hour) to handle multi-day data
    by_date_hour: dict[tuple[str, int], list[dict]] = {}
    for obs in observations:
        hour = obs.get("Hour")
        obs_date = obs.get("Date", "")
        if hour is not None:
            by_date_hour.setdefault((obs_date, hour), []).append(obs)

    if not by_date_hour:
        return [], "", ""

    # Try date+hour combos from most recent backwards
    sorted_keys = sorted(by_date_hour.keys(), reverse=True)
    for date_hour_key in sorted_keys[:12]:
        hour_obs = by_date_hour[date_hour_key]
        readings = []
        obs_time = ""
        obs_date = ""
        hour = date_hour_key[1]

        for obs in hour_obs:
            param_code = obs.get("Parameter", {}).get("ParameterCode", "") if isinstance(obs.get("Parameter"), dict) else obs.get("Parameter", "")
            value = obs.get("Value")
            category = obs.get("AirQualityCategory", "")

            if value is None:
                continue

            if pollutant_filter and param_code != pollutant_filter:
                continue

            readings.append({
                "parameter": param_code,
                "value": value,
                "unit": POLLUTANT_UNITS.get(param_code, ""),
                "category": category,
            })

            if not obs_time:
                obs_time = obs.get("HourDescription", f"{hour:02d}:00-{(hour + 1) % 24:02d}:00")
            if not obs_date:
                obs_date = obs.get("Date", date.today().isoformat())

        if readings:
            return readings, obs_time, obs_date

    return [], "", ""


def _normalize_category(cat: str) -> str:
    """Normalize AQI category from API (e.g., 'GOOD' → 'Good')."""
    if not cat:
        return ""
    cat_lower = cat.strip().lower()
    for c in CATEGORY_ORDER:
        if c.lower() == cat_lower:
            return c
    return cat.title()


def determine_overall_category(readings: list[dict]) -> str:
    """Determine the worst AQI category across all readings."""
    worst_idx = 0
    for reading in readings:
        cat = _normalize_category(reading.get("category", ""))
        reading["category"] = cat  # normalize in-place for output
        if cat in CATEGORY_ORDER:
            idx = CATEGORY_ORDER.index(cat)
            if idx > worst_idx:
                worst_idx = idx
    return CATEGORY_ORDER[worst_idx] if readings else "Good"


def detect_bushfire_smoke(readings: list[dict]) -> bool:
    """Detect likely bushfire smoke conditions.

    Criteria: PM2.5 > 25 µg/m³ AND (NEPH > 2.0 OR PM2.5 > 50)
    """
    pm25_val = None
    neph_val = None

    for reading in readings:
        if reading["parameter"] == "PM2.5":
            pm25_val = reading["value"]
        elif reading["parameter"] == "NEPH":
            neph_val = reading["value"]

    if pm25_val is None:
        return False

    if pm25_val > BUSHFIRE_PM25_THRESHOLD:
        if pm25_val > BUSHFIRE_PM25_HIGH:
            return True
        if neph_val is not None and neph_val > BUSHFIRE_NEPH_THRESHOLD:
            return True

    return False


# ---------------------------------------------------------------------------
# Error output
# ---------------------------------------------------------------------------


def _err(message: str, **extra) -> None:
    """Print a JSON error to stdout and exit."""
    out = {"error": message}
    out.update(extra)
    print(json.dumps(out, indent=2))
    sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check air quality at NSW monitoring stations")
    parser.add_argument("--location", "-l", help="Suburb or city name (e.g., 'Randwick')")
    parser.add_argument("--lat", type=float, help="Latitude")
    parser.add_argument("--lng", type=float, help="Longitude")
    parser.add_argument("--site", "-s", help="Monitoring site name (direct lookup)")
    parser.add_argument(
        "--pollutant",
        "-p",
        choices=ALL_POLLUTANTS,
        help="Filter to a specific pollutant",
    )
    parser.add_argument("--no-cache", action="store_true", help="Force fresh data")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    import httpx

    args = parse_args()

    # Build cache key
    cache_key = f"result_{args.location}_{args.lat}_{args.lng}_{args.site}_{args.pollutant}"
    if args.no_cache:
        # Clear location and result caches
        loc_cache = CACHE_DIR / "location.json"
        if loc_cache.exists():
            loc_cache.unlink()
        result_cache = _cache_path(cache_key)
        if result_cache.exists():
            result_cache.unlink()
    else:
        cached = cache_get(cache_key, OBS_CACHE_TTL)
        if cached:
            print(json.dumps(cached, indent=2))
            return

    async with httpx.AsyncClient() as client:
        # 1. Fetch site list
        sites = await fetch_site_details(client, no_cache=args.no_cache)
        if not sites:
            _err("Could not fetch monitoring site list from NSW Air Quality API.")

        # 2. Resolve site
        target_site = None
        distance_km = 0.0
        query_mode = "general"
        location = None

        if args.site:
            # Direct site lookup by name
            query_mode = "site"
            target_site = fuzzy_match_site(sites, args.site)
            if not target_site:
                available = sorted(set(s.get("SiteName", "") for s in sites if s.get("SiteName")))
                _err(
                    f"No monitoring site matching '{args.site}' found.",
                    available_sites=available[:20],
                    suggestion="Try one of the listed site names, or use --location for nearest-site lookup.",
                )
            # Build a location from the site itself
            location = Location(
                lat=target_site.get("Latitude", 0),
                lng=target_site.get("Longitude", 0),
                city=target_site.get("SiteName", ""),
                state="NSW",
                postcode="",
                country="Australia",
                method="site-lookup",
            )
            distance_km = 0.0
        else:
            # Resolve user location
            location = await location_from_args(args, client)
            if not location:
                _err("Could not determine location. Use --location, --lat/--lng, or --site.")

            # Find nearest site
            target_site, distance_km = find_nearest_site(sites, location.lat, location.lng)
            if not target_site or distance_km > MAX_SITE_DISTANCE_KM:
                _err(
                    f"No monitoring site found within {MAX_SITE_DISTANCE_KM}km of your location.",
                    location={"city": location.city, "lat": location.lat, "lng": location.lng},
                    suggestion="Try --site to query a specific monitoring station, or use --location with a Sydney suburb.",
                )

        # Flag IP-only detection so the agent knows accuracy is limited
        if location.method in ("ip-api.com", "ip-fallback") and not (args.lat and args.lng):
            location_confidence = "low"
        else:
            location_confidence = "high"

        # 3. Fetch observations
        site_id = target_site["Site_Id"]
        pollutants = [args.pollutant] if args.pollutant else ALL_POLLUTANTS
        observations = await fetch_observations(client, site_id, pollutants, no_cache=args.no_cache)

    if not observations:
        _err(
            f"No air quality data available for {target_site.get('SiteName', 'this site')} in the last 2 days.",
            site=target_site.get("SiteName", ""),
            suggestion="Try a different site with --site, or check back later.",
        )

    # 4. Extract latest readings
    readings, obs_time, obs_date = extract_latest_readings(observations, args.pollutant)

    if not readings:
        _err(
            f"No readings available for {target_site.get('SiteName', 'this site')} in the last 6 hours.",
            site=target_site.get("SiteName", ""),
            suggestion="Try a different site with --site, or check back later.",
        )

    # 5. Compute overall category + advice
    overall_category = determine_overall_category(readings)
    health_advice = HEALTH_ADVICE.get(overall_category, "")
    exercise_advice = EXERCISE_ADVICE.get(overall_category, "")

    # 6. Detect bushfire smoke
    bushfire_smoke = detect_bushfire_smoke(readings)
    bushfire_advisory = None
    if bushfire_smoke:
        bushfire_advisory = (
            "Elevated PM2.5 and reduced visibility suggest bushfire smoke in the area. "
            "Avoid outdoor activity. Close windows and use air purifiers if available."
        )

    # 7. Build output
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
        "query": {
            "mode": query_mode,
            "site": target_site.get("SiteName", ""),
            "site_id": site_id,
            "distance_km": round(distance_km, 1),
            "pollutant_filter": args.pollutant,
        },
        "results": {
            "overall_category": overall_category,
            "health_advice": health_advice,
            "exercise_advice": exercise_advice,
            "bushfire_smoke": bushfire_smoke,
            "observation_time": obs_time,
            "observation_date": obs_date,
            "readings": readings,
            "bushfire_advisory": bushfire_advisory,
            "site_info": {
                "name": target_site.get("SiteName", ""),
                "region": target_site.get("Region", ""),
                "lat": target_site.get("Latitude"),
                "lng": target_site.get("Longitude"),
            },
        },
    }

    if location_confidence == "low":
        result["location"]["note"] = (
            "Location was detected via IP address only (city-level accuracy). "
            "The user may not actually be in this area. Ask them to confirm their "
            "suburb or postcode for accurate results."
        )

    # Cache result
    if not args.no_cache:
        cache_set(cache_key, result)

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
