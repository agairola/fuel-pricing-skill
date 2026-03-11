#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27.0",
# ]
# ///
"""
Sydney Commute — plan trips, check departures, find stops on Sydney's public transport.

Zero-config: works immediately with no API keys (provides Google Maps / TfNSW links).
Optional: save TfNSW API key to ~/.config/sydney-commute/credentials.json for real-time data.

Usage:
    uv run commute.py --from "Central Station" --to "Bondi Junction"
    uv run commute.py --mode departures --from "Central Station"
    uv run commute.py --mode stops --from "Central"
    uv run commute.py --from "Town Hall" --to "Circular Quay" --transport train
    uv run commute.py --from "here" --to "Manly" --lat -33.87 --lng 151.21
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
from urllib.parse import quote, urlencode

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

PRODUCT_CLASS_MAP = {
    1: "train",
    4: "lightrail",
    5: "bus",
    7: "coach",
    9: "ferry",
    11: "school_bus",
    99: "walk",
    100: "walk",
}

TRANSPORT_FILTER = {
    "train": [1],
    "bus": [5],
    "ferry": [9],
    "lightrail": [4],
    "metro": [1],  # metro uses same product class as train in TfNSW
    "coach": [7],
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

CACHE_DIR = Path.home() / ".config" / "sydney-commute"
CREDENTIALS_PATH = CACHE_DIR / "credentials.json"
CACHE_TTL_SECONDS = 60  # real-time data, short TTL


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


def save_credentials(api_key: str) -> None:
    """Save API credentials to file with restricted permissions."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    creds = {}
    if CREDENTIALS_PATH.exists():
        try:
            creds = json.loads(CREDENTIALS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    creds["tfnsw_api_key"] = api_key
    CREDENTIALS_PATH.write_text(json.dumps(creds, indent=2))
    CREDENTIALS_PATH.chmod(0o600)


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
<html><head><title>Sydney Commute - Location</title>
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
  <h2>Sydney Commute</h2>
  <p>Allow location access to find transport near you.</p>
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


NOMINATIM_HEADERS = {"User-Agent": "sydney-commute-cli/1.0"}


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
                    city=rev["city"],
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
                city=ip_loc.city if ip_loc else "",
                state=ip_loc.state if ip_loc else "",
                postcode=ip_loc.postcode if ip_loc else "",
                country="AU",
                method="manual",
            )
        # Try to geocode from --from arg if it looks like a place name
        origin = getattr(args, "from_location", None) or ""
        if origin and not origin.isdigit():
            geo_loc = await _geocode_forward(client, origin)
            if geo_loc:
                return geo_loc
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
# Error output helper
# ---------------------------------------------------------------------------


def _error_json(message: str, **extra) -> str:
    """Return a JSON error string to stdout."""
    data = {"error": message}
    data.update(extra)
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------


def _google_maps_transit_url(origin: str, destination: str, departure_unix: int | None = None) -> str:
    """Build a Google Maps directions URL with transit mode."""
    params = {
        "api": 1,
        "origin": origin,
        "destination": destination,
        "travelmode": "transit",
    }
    if departure_unix:
        params["departure_time"] = departure_unix
    return f"https://www.google.com/maps/dir/?{urlencode(params)}"


def _transport_nsw_url(origin: str, destination: str) -> str:
    """Build a Transport NSW trip planner URL."""
    return f"https://transportnsw.info/trip#/trip?from={quote(origin)}&to={quote(destination)}"


# ---------------------------------------------------------------------------
# TfNSW API
# ---------------------------------------------------------------------------

API_BASE = "https://api.transport.nsw.gov.au/v1/tp"


def _tfnsw_headers(api_key: str) -> dict:
    """Build auth headers for TfNSW API."""
    return {"Authorization": f"apikey {api_key}"}


def _now_date_time() -> tuple[str, str]:
    """Return current date and time as (YYYYMMDD, HHMM) strings."""
    now = time.localtime()
    return time.strftime("%Y%m%d", now), time.strftime("%H%M", now)


def _parse_time_arg(time_str: str) -> tuple[str, str]:
    """Parse a time argument (HH:MM or 'now') into (YYYYMMDD, HHMM)."""
    if time_str.lower() == "now":
        return _now_date_time()
    # Parse HH:MM
    parts = time_str.split(":")
    if len(parts) == 2:
        hh, mm = parts[0].zfill(2), parts[1].zfill(2)
        date_str = time.strftime("%Y%m%d", time.localtime())
        return date_str, f"{hh}{mm}"
    # Fallback to now
    return _now_date_time()


def _parse_iso_time(iso_str: str | None) -> str | None:
    """Parse ISO datetime string to HH:MM format."""
    if not iso_str:
        return None
    try:
        # Handle ISO format: 2024-01-15T14:32:00+11:00
        # Just extract the time portion
        if "T" in iso_str:
            time_part = iso_str.split("T")[1]
            return time_part[:5]  # HH:MM
        return iso_str[:5]
    except (IndexError, ValueError):
        return None


def _calc_delay_min(planned: str | None, estimated: str | None) -> int | None:
    """Calculate delay in minutes between planned and estimated times."""
    if not planned or not estimated:
        return None
    try:
        # Parse ISO datetimes
        p_time = _parse_iso_time(planned)
        e_time = _parse_iso_time(estimated)
        if not p_time or not e_time:
            return None
        p_h, p_m = int(p_time[:2]), int(p_time[3:5])
        e_h, e_m = int(e_time[:2]), int(e_time[3:5])
        diff = (e_h * 60 + e_m) - (p_h * 60 + p_m)
        # Handle midnight crossing
        if diff < -720:
            diff += 1440
        return diff if diff != 0 else None
    except (ValueError, IndexError):
        return None


def _product_classes_to_types(classes: list | None) -> list[str]:
    """Convert product class numbers to human-readable transport types."""
    if not classes:
        return []
    types = []
    for c in classes:
        t = PRODUCT_CLASS_MAP.get(c)
        if t and t not in types:
            types.append(t)
    return types


def _transport_mode_name(product_name: str | None, product_class: int | None = None) -> str:
    """Extract a clean transport mode name."""
    if product_class is not None:
        mapped = PRODUCT_CLASS_MAP.get(product_class, "")
        if mapped:
            return mapped.title()
    if product_name:
        name_lower = product_name.lower()
        if "train" in name_lower:
            return "Train"
        if "bus" in name_lower:
            return "Bus"
        if "ferry" in name_lower:
            return "Ferry"
        if "light rail" in name_lower or "lightrail" in name_lower:
            return "Light Rail"
        if "metro" in name_lower:
            return "Metro"
        if "coach" in name_lower:
            return "Coach"
        return product_name
    return "Unknown"


# ---------------------------------------------------------------------------
# Trip mode
# ---------------------------------------------------------------------------


async def _resolve_stop_id(
    client: "httpx.AsyncClient",
    api_key: str,
    name: str,
) -> tuple[str, str]:
    """Resolve a stop name to a stop ID via stop_finder.

    Returns (stop_id_or_name, resolved_name). If the name is already a numeric
    stop ID, returns it as-is. If resolution fails, returns the original name
    so the trip API can attempt its own matching.
    """
    if name.isdigit():
        return name, name
    try:
        resp = await client.get(
            f"{API_BASE}/stop_finder",
            params={
                "outputFormat": "rapidJSON",
                "coordOutputFormat": "EPSG:4326",
                "type_sf": "any",
                "name_sf": name,
                "TfNSWSF": "true",
            },
            headers=_tfnsw_headers(api_key),
            timeout=10,
        )
        if resp.status_code == 200:
            locations = resp.json().get("locations", [])
            # Prefer stops/platforms over POIs and streets
            for loc in locations:
                loc_type = loc.get("type", "")
                if loc_type in ("stop", "platform"):
                    resolved_id = loc.get("id", name)
                    resolved_name = loc.get("name", name)
                    print(f"Resolved '{name}' -> {resolved_name} ({resolved_id})", file=sys.stderr)
                    return resolved_id, resolved_name
            # Fall back to first result if no stop type found
            if locations:
                resolved_id = locations[0].get("id", name)
                resolved_name = locations[0].get("name", name)
                print(f"Resolved '{name}' -> {resolved_name} ({resolved_id})", file=sys.stderr)
                return resolved_id, resolved_name
    except Exception:
        pass
    return name, name


async def fetch_trip(
    client: "httpx.AsyncClient",
    api_key: str,
    origin: str,
    destination: str,
    depart_date: str,
    depart_time: str,
    arrive_by: bool = False,
    transport_filter: str | None = None,
) -> dict:
    """Fetch trip planning results from TfNSW API."""
    # Resolve stop names to IDs for reliable matching
    origin_id, origin_name = await _resolve_stop_id(client, api_key, origin)
    dest_id, dest_name = await _resolve_stop_id(client, api_key, destination)

    params = {
        "outputFormat": "rapidJSON",
        "coordOutputFormat": "EPSG:4326",
        "depArrMacro": "arr" if arrive_by else "dep",
        "itdDate": depart_date,
        "itdTime": depart_time,
        "type_origin": "any",
        "name_origin": origin_id,
        "type_destination": "any",
        "name_destination": dest_id,
    }

    try:
        resp = await client.get(
            f"{API_BASE}/trip",
            params=params,
            headers=_tfnsw_headers(api_key),
            timeout=15,
        )

        if resp.status_code == 401:
            return {
                "error": "TfNSW API key is invalid. Please check your credentials.",
                "registration_url": "https://opendata.transport.nsw.gov.au",
            }

        if resp.status_code != 200:
            return {"error": f"TfNSW API returned status {resp.status_code}"}

        data = resp.json()
        journeys = data.get("journeys", [])

        if not journeys:
            return {"count": 0, "journeys": []}

        results = []
        for journey in journeys:
            legs_data = journey.get("legs", [])
            legs = []
            mode_summary = []

            for leg in legs_data:
                origin_info = leg.get("origin", {})
                dest_info = leg.get("destination", {})
                transport = leg.get("transportation", {})
                product = transport.get("product", {})
                product_class = product.get("class")

                mode = _transport_mode_name(product.get("name"), product_class)

                # Skip walk legs in summary if very short
                duration_sec = leg.get("duration", 0)
                is_walk = product_class in (99, 100) or mode.lower() == "walk"

                if not is_walk:
                    mode_summary.append(mode)

                leg_info = {
                    "mode": mode,
                    "line": transport.get("description") or transport.get("number") or "",
                    "from": origin_info.get("name", ""),
                    "from_platform": origin_info.get("platformName", ""),
                    "to": dest_info.get("name", ""),
                    "depart": _parse_iso_time(
                        origin_info.get("departureTimeEstimated")
                        or origin_info.get("departureTimePlanned")
                    ),
                    "arrive": _parse_iso_time(
                        dest_info.get("arrivalTimeEstimated")
                        or dest_info.get("arrivalTimePlanned")
                    ),
                    "duration_min": round(duration_sec / 60) if duration_sec else None,
                    "stops": 0,
                    "realtime": bool(
                        origin_info.get("departureTimeEstimated")
                        or dest_info.get("arrivalTimeEstimated")
                    ),
                }

                # Count intermediate stops
                stop_seq = leg.get("stopSequence", [])
                if isinstance(stop_seq, list) and len(stop_seq) > 2:
                    leg_info["stops"] = len(stop_seq) - 2  # exclude origin and destination
                else:
                    leg_info["stops"] = 0

                # Apply transport filter — skip entire journey if any non-walk leg doesn't match
                if transport_filter:
                    allowed = TRANSPORT_FILTER.get(transport_filter, [])
                    if allowed and product_class not in allowed and not is_walk:
                        break

                legs.append(leg_info)

            if not legs:
                continue

            # Calculate total journey time
            first_depart = legs[0].get("depart")
            last_arrive = legs[-1].get("arrive")
            total_duration = None
            if first_depart and last_arrive:
                try:
                    fd_h, fd_m = int(first_depart[:2]), int(first_depart[3:5])
                    la_h, la_m = int(last_arrive[:2]), int(last_arrive[3:5])
                    total_duration = (la_h * 60 + la_m) - (fd_h * 60 + fd_m)
                    if total_duration < 0:
                        total_duration += 1440  # midnight crossing
                except (ValueError, IndexError):
                    pass

            journey_result = {
                "summary": " -> ".join(mode_summary) if mode_summary else "Walk",
                "duration_min": total_duration,
                "departure": first_depart,
                "arrival": last_arrive,
                "legs": legs,
                "google_maps_url": _google_maps_transit_url(origin, destination),
                "transport_nsw_url": _transport_nsw_url(origin, destination),
            }
            results.append(journey_result)

        return {"count": len(results), "journeys": results}

    except Exception as e:
        print(f"Trip API error: {e}", file=sys.stderr)
        return {"error": f"Failed to fetch trip data: {e}"}


# ---------------------------------------------------------------------------
# Departures mode
# ---------------------------------------------------------------------------


async def fetch_departures(
    client: "httpx.AsyncClient",
    api_key: str,
    stop_id: str,
    depart_date: str,
    depart_time: str,
    transport_filter: str | None = None,
) -> dict:
    """Fetch departure board from TfNSW API."""
    params = {
        "outputFormat": "rapidJSON",
        "coordOutputFormat": "EPSG:4326",
        "mode": "direct",
        "type_dm": "stop",
        "name_dm": stop_id,
        "depArrMacro": "dep",
        "itdDate": depart_date,
        "itdTime": depart_time,
        "TfNSWDM": "true",
    }

    try:
        resp = await client.get(
            f"{API_BASE}/departure_mon",
            params=params,
            headers=_tfnsw_headers(api_key),
            timeout=15,
        )

        if resp.status_code == 401:
            return {
                "error": "TfNSW API key is invalid. Please check your credentials.",
                "registration_url": "https://opendata.transport.nsw.gov.au",
            }

        if resp.status_code != 200:
            return {"error": f"TfNSW API returned status {resp.status_code}"}

        data = resp.json()
        stop_events = data.get("stopEvents", [])

        if not stop_events:
            return {"stop_name": "", "count": 0, "departures": []}

        stop_name = ""
        departures = []
        for event in stop_events:
            location = event.get("location", {})
            transport = event.get("transportation", {})
            product = transport.get("product", {})
            product_class = product.get("class")

            if not stop_name:
                stop_name = location.get("name", "")

            # Apply transport filter
            if transport_filter:
                allowed = TRANSPORT_FILTER.get(transport_filter, [])
                if allowed and product_class not in allowed:
                    continue

            planned = event.get("departureTimePlanned")
            estimated = event.get("departureTimeEstimated")
            delay = _calc_delay_min(planned, estimated)

            departure = {
                "line": transport.get("description") or transport.get("number") or "",
                "destination": transport.get("destination", {}).get("name", ""),
                "scheduled": _parse_iso_time(planned),
                "estimated": _parse_iso_time(estimated),
                "delay_min": delay,
                "platform": location.get("platformName", ""),
                "realtime": event.get("isRealtimeControlled", False),
            }
            departures.append(departure)

        return {
            "stop_name": stop_name,
            "count": len(departures),
            "departures": departures,
        }

    except Exception as e:
        print(f"Departures API error: {e}", file=sys.stderr)
        return {"error": f"Failed to fetch departure data: {e}"}


# ---------------------------------------------------------------------------
# Stops mode
# ---------------------------------------------------------------------------


async def fetch_stops(
    client: "httpx.AsyncClient",
    api_key: str,
    search_term: str,
    transport_filter: str | None = None,
) -> dict:
    """Search for stops/stations via TfNSW API."""
    params = {
        "outputFormat": "rapidJSON",
        "coordOutputFormat": "EPSG:4326",
        "type_sf": "any",
        "name_sf": search_term,
        "TfNSWSF": "true",
    }

    try:
        resp = await client.get(
            f"{API_BASE}/stop_finder",
            params=params,
            headers=_tfnsw_headers(api_key),
            timeout=15,
        )

        if resp.status_code == 401:
            return {
                "error": "TfNSW API key is invalid. Please check your credentials.",
                "registration_url": "https://opendata.transport.nsw.gov.au",
            }

        if resp.status_code != 200:
            return {"error": f"TfNSW API returned status {resp.status_code}"}

        data = resp.json()
        locations = data.get("locations", [])

        if not locations:
            return {"count": 0, "stops": []}

        stops = []
        for loc in locations:
            coord = loc.get("coord", [])
            lat = coord[1] if len(coord) > 1 else None
            lng = coord[0] if len(coord) > 0 else None

            product_classes = loc.get("productClasses", [])
            transport_types = _product_classes_to_types(product_classes)

            # Apply transport filter
            if transport_filter and transport_filter not in transport_types:
                continue

            stop = {
                "id": loc.get("id", ""),
                "name": loc.get("name", ""),
                "type": loc.get("type", ""),
                "lat": lat,
                "lng": lng,
                "transport_types": transport_types,
            }
            stops.append(stop)

        return {"count": len(stops), "stops": stops}

    except Exception as e:
        print(f"Stops API error: {e}", file=sys.stderr)
        return {"error": f"Failed to fetch stops data: {e}"}


# ---------------------------------------------------------------------------
# Zero-config fallback (no API key)
# ---------------------------------------------------------------------------


def _zero_config_result(
    origin: str,
    destination: str,
    mode: str,
    depart_time: str | None = None,
) -> dict:
    """Build a zero-config fallback result with useful URLs."""
    # Calculate departure unix timestamp for Google Maps
    departure_unix = None
    if depart_time and depart_time.lower() != "now":
        try:
            parts = depart_time.split(":")
            if len(parts) == 2:
                now = time.localtime()
                hh, mm = int(parts[0]), int(parts[1])
                import calendar
                target = time.mktime(time.struct_time((
                    now.tm_year, now.tm_mon, now.tm_mday,
                    hh, mm, 0, 0, 0, now.tm_isdst,
                )))
                if target < time.time():
                    target += 86400  # tomorrow
                departure_unix = int(target)
        except (ValueError, IndexError):
            pass

    result = {
        "query": {
            "from": origin or "",
            "to": destination or "",
            "mode": mode,
        },
        "api_key_configured": False,
        "fallback_urls": {},
        "upgrade_nudge": (
            "For real-time departure data, delays, and trip planning, "
            "register for a free TfNSW API key (~2 minutes). See the setup instructions."
        ),
    }

    if origin and destination:
        result["fallback_urls"]["google_maps"] = _google_maps_transit_url(
            origin, destination, departure_unix
        )
        result["fallback_urls"]["transport_nsw"] = _transport_nsw_url(
            origin, destination
        )
    elif origin:
        # For departures/stops, provide what we can
        result["fallback_urls"]["transport_nsw"] = (
            f"https://transportnsw.info/trip#/trip?from={quote(origin)}"
        )
        result["fallback_urls"]["google_maps"] = (
            f"https://www.google.com/maps/search/{quote(origin)}+station"
        )

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sydney public transport: trips, departures, stop search"
    )
    parser.add_argument(
        "--from", dest="from_location",
        help="Origin location/stop (e.g., 'Central Station')",
    )
    parser.add_argument(
        "--to", dest="to_location",
        help="Destination location/stop (e.g., 'Bondi Junction')",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["trip", "departures", "stops"],
        default="trip",
        help="Mode: trip (default), departures, or stops",
    )
    parser.add_argument(
        "--depart", "-d",
        default="now",
        help="Departure time (HH:MM or 'now', default: now)",
    )
    parser.add_argument(
        "--arrive-by",
        help="Arrive by time (HH:MM)",
    )
    parser.add_argument(
        "--transport", "-t",
        choices=["train", "bus", "ferry", "lightrail", "metro", "coach"],
        help="Filter by transport type",
    )
    parser.add_argument("--lat", type=float, help="Latitude")
    parser.add_argument("--lng", type=float, help="Longitude")
    parser.add_argument("--no-cache", action="store_true", help="Skip cache")
    return parser.parse_args()


async def main() -> None:
    import httpx

    args = parse_args()
    mode = args.mode
    origin = args.from_location or ""
    destination = args.to_location or ""
    transport = args.transport

    # Determine departure time
    arrive_by = False
    if args.arrive_by:
        depart_date, depart_time = _parse_time_arg(args.arrive_by)
        arrive_by = True
    else:
        depart_date, depart_time = _parse_time_arg(args.depart)

    # Build cache key -- include credentials presence so adding/removing keys invalidates cache
    creds = _get_credentials()
    api_key = creds.get("tfnsw_api_key", "")
    has_creds = "1" if api_key else "0"
    cache_key = f"commute_{mode}_{origin}_{destination}_{depart_time}_{transport}_{has_creds}"

    if args.no_cache:
        # Clear both location and result cache
        loc_cache = CACHE_DIR / "location.json"
        if loc_cache.exists():
            loc_cache.unlink()
        result_cache = _cache_path(cache_key)
        if result_cache.exists():
            result_cache.unlink()
    else:
        cached = cache_get(cache_key)
        if cached:
            print(json.dumps(cached, indent=2))
            return

    # Zero-config fallback when no API key
    if not api_key:
        result = _zero_config_result(origin, destination, mode, args.depart)
        print(json.dumps(result, indent=2))
        return

    # Resolve location for context — only needed for departures mode
    # Trip and stops modes use explicit --from/--to, no geolocation needed
    async with httpx.AsyncClient() as client:
        location = None
        location_confidence = "high"
        if mode == "departures":
            location = await location_from_args(args, client)
            if location and location.method in ("ip-api.com", "ip-fallback"):
                if not (args.lat and args.lng):
                    location_confidence = "low"

        # Execute the appropriate mode
        if mode == "trip":
            if not origin or not destination:
                print(_error_json(
                    "Trip mode requires both --from and --to arguments.",
                    hint="Example: --from 'Central Station' --to 'Bondi Junction'",
                ))
                sys.exit(1)

            results = await fetch_trip(
                client, api_key, origin, destination,
                depart_date, depart_time, arrive_by, transport,
            )

            output = {
                "query": {
                    "from": origin,
                    "to": destination,
                    "mode": "trip",
                    "depart": args.arrive_by if arrive_by else args.depart,
                },
                "api_key_configured": True,
                "results": results,
            }

        elif mode == "departures":
            if not origin:
                print(_error_json(
                    "Departures mode requires --from argument (stop name or ID).",
                    hint="Example: --from 'Central Station' or --from '10101200'",
                ))
                sys.exit(1)

            # If origin looks like a stop ID (all digits), use it directly
            # Otherwise, search for stops first to resolve the ID
            stop_id = origin
            stop_name = origin
            if not origin.isdigit():
                # Search for the stop first
                stop_results = await fetch_stops(client, api_key, origin, transport)
                if "error" in stop_results:
                    print(json.dumps(stop_results, indent=2))
                    sys.exit(1)
                stops = stop_results.get("stops", [])
                if not stops:
                    print(_error_json(
                        f"No stops found matching '{origin}'.",
                        hint="Try a more specific stop name.",
                    ))
                    sys.exit(1)
                if len(stops) > 1:
                    # Multiple matches -- use the first but include alternatives
                    stop_id = stops[0]["id"]
                    stop_name = stops[0]["name"]
                    print(
                        f"Multiple stops found, using: {stop_name} ({stop_id})",
                        file=sys.stderr,
                    )
                else:
                    stop_id = stops[0]["id"]
                    stop_name = stops[0]["name"]

            results = await fetch_departures(
                client, api_key, stop_id,
                depart_date, depart_time, transport,
            )

            output = {
                "query": {
                    "stop": stop_name,
                    "stop_id": stop_id,
                    "mode": "departures",
                },
                "api_key_configured": True,
                "results": results,
            }

        elif mode == "stops":
            search_term = origin or destination or ""
            if not search_term:
                print(_error_json(
                    "Stops mode requires --from argument (search term).",
                    hint="Example: --from 'Central'",
                ))
                sys.exit(1)

            results = await fetch_stops(client, api_key, search_term, transport)

            output = {
                "query": {
                    "search": search_term,
                    "mode": "stops",
                },
                "api_key_configured": True,
                "results": results,
            }

        else:
            print(_error_json(f"Unknown mode: {mode}"))
            sys.exit(1)

        # Add location context if available
        if location:
            output["location"] = {
                "city": location.city,
                "state": location.state,
                "postcode": location.postcode,
                "country": location.country,
                "lat": location.lat,
                "lng": location.lng,
                "confidence": location_confidence,
            }
            if location_confidence == "low":
                output["location"]["note"] = (
                    "Location was detected via IP address only (city-level accuracy). "
                    "The user may not actually be in this area. Ask them to confirm their "
                    "suburb or postcode for accurate results."
                )

        # Cache result
        if not args.no_cache:
            cache_set(cache_key, output)

        print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
