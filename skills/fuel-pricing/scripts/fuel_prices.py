#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27.0",
# ]
# ///
"""
Fuel Pricing Australia — find cheapest fuel near you.

Zero-config: works immediately with no API keys.
Optional: save FuelCheck API keys to ~/.config/fuel-pricing/credentials.json for official NSW govt data.

Usage:
    uv run fuel_prices.py                          # auto-detect location
    uv run fuel_prices.py --location "Newtown NSW" # specify suburb
    uv run fuel_prices.py --postcode 2042          # specify postcode
    uv run fuel_prices.py --fuel-type E10          # filter fuel type
    uv run fuel_prices.py --radius 10              # search radius in km
    uv run fuel_prices.py --lat -33.8 --lng 151.2  # exact coordinates
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
from functools import reduce
from pathlib import Path
from typing import Callable
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

MAX_SANE_PRICE = 5.0   # $/L — anything above this is almost certainly bad data
MIN_SANE_PRICE = 0.50  # $/L — anything below this is almost certainly bad data

FUEL_TYPE_NAMES = {
    "E10": "Ethanol 10%",
    "U91": "Unleaded 91",
    "U95": "Premium 95",
    "U98": "Premium 98",
    "DSL": "Diesel",
    "PDSL": "Premium Diesel",
    "LPG": "LPG",
    "EV": "Electric",
}

# Normalisation maps from source-specific fuel names to our codes
FUELWATCH_FUEL_MAP = {
    "ULP": "U91",
    "PULP": "U95",
    "98 RON": "U98",
    "Diesel": "DSL",
    "Brand Diesel": "PDSL",
    "LPG": "LPG",
    "E85": "E10",  # closest match
}

FUELSNOOP_FUEL_MAP = {
    "E10": "E10",
    "U91": "U91",
    "U95": "U95",
    "U98": "U98",
    "DSL": "DSL",
    "PDSL": "PDSL",
    "LPG": "LPG",
}

PETROLSPY_FUEL_MAP = {
    "E10": "E10",
    "U91": "U91",
    "U95": "U95",
    "U98": "U98",
    "Diesel": "DSL",
    "DIESEL": "DSL",
    "diesel": "DSL",
    "TruckDSL": "DSL",
    "PremDSL": "PDSL",
    "LPG": "LPG",
    "EV": "EV",
    "E85": "E10",
    "AdBlue": None,  # not a fuel type we track
}

# NSW FuelCheck API fuel codes → our codes
FUELCHECK_FUEL_MAP = {
    "E10": "E10",
    "U91": "U91",
    "P95": "U95",
    "P98": "U98",
    "DL": "DSL",
    "PDL": "PDSL",
    "LPG": "LPG",
    "E85": "E10",
    "B20": "DSL",
    "EV": "EV",
    "CNG": None,
    "LNG": None,
    "HYD": None,
}


@dataclass
class Station:
    name: str
    brand: str
    address: str
    suburb: str
    state: str
    postcode: str
    lat: float
    lng: float
    prices: dict[str, float | None]  # fuel_code -> price in dollars
    updated_at: str  # ISO or human-readable
    source: str
    distance_km: float | None = None
    price_tomorrow: dict[str, float | None] | None = None


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

CACHE_DIR = Path.home() / ".config" / "fuel-pricing"
CREDENTIALS_PATH = CACHE_DIR / "credentials.json"
CACHE_TTL_SECONDS = 300  # 5 minutes


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
    # 1. Try credentials file (preferred — chmod 600, not in shell env)
    if CREDENTIALS_PATH.exists():
        try:
            creds = json.loads(CREDENTIALS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    # 2. Fall back to env vars for backwards compatibility
    if not creds.get("fuelcheck_key"):
        key = os.environ.get("FUELCHECK_CONSUMER_KEY", "")
        secret = os.environ.get("FUELCHECK_CONSUMER_SECRET", "")
        if key and secret:
            creds["fuelcheck_key"] = key
            creds["fuelcheck_secret"] = secret
    return creds


def save_credentials(key: str, secret: str) -> None:
    """Save API credentials to file with restricted permissions."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    creds = {}
    if CREDENTIALS_PATH.exists():
        try:
            creds = json.loads(CREDENTIALS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    creds["fuelcheck_key"] = key
    creds["fuelcheck_secret"] = secret
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
<html><head><title>Fuel Pricing - Location</title>
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
  <h2>Fuel Price Finder</h2>
  <p>Allow location access to find cheap fuel near you.</p>
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


NOMINATIM_HEADERS = {"User-Agent": "fuel-pricing-cli/1.0"}


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

    The browser approach works like `gh auth login` — opens a localhost page that
    requests navigator.geolocation, which uses the OS's WiFi positioning system.
    Same accuracy as Find My Mac / Google Maps in the browser.
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
            state = _state_from_coords(args.lat, args.lng)
            if rev:
                return Location(
                    lat=args.lat,
                    lng=args.lng,
                    city=args.location or rev["city"],
                    state=rev["state"] or state,
                    postcode=str(args.postcode or rev["postcode"]),
                    country=rev["country"],
                    method="manual",
                )
            # Fall back to IP enrichment if Nominatim fails
            ip_loc = await _geolocate_ip(client)
            return Location(
                lat=args.lat,
                lng=args.lng,
                city=args.location or (ip_loc.city if ip_loc else ""),
                state=state or (ip_loc.state if ip_loc else ""),
                postcode=str(args.postcode or ""),
                country="AU",
                method="manual",
            )
        if args.location or args.postcode:
            # Forward geocode to get accurate coords for the place
            query = args.location or ""
            if args.postcode:
                query = f"{args.postcode}, Australia" if not query else query
            geo_loc = await _geocode_forward(client, query)
            if geo_loc:
                return geo_loc
            # Fall back to IP-based behavior if Nominatim fails
            print(f"Warning: geocoding failed for '{query}', falling back to IP geolocation", file=sys.stderr)
            ip_loc = await _geolocate_ip(client)
            if ip_loc:
                ip_loc.city = args.location or ip_loc.city
                ip_loc.postcode = str(args.postcode) if args.postcode else ip_loc.postcode
                ip_loc.method = "ip-fallback"
                return ip_loc
            # Can't geolocate at all — return a stub
            return Location(
                lat=0,
                lng=0,
                city=args.location or "",
                state="",
                postcode=str(args.postcode) if args.postcode else "",
                country="AU",
                method="manual",
            )
        # Auto-detect
        return await geolocate(client)

    return _resolve()


def _state_from_coords(lat: float, lng: float) -> str:
    """Rough state detection from coordinates. Good enough for routing."""
    # Tasmania: lat < -39.5 and lng 143.5-149 (check first — overlaps with VIC lng bands)
    if lat < -39.5 and 143.5 < lng < 149:
        return "TAS"
    if lng < 129:
        return "WA"
    if lng < 138:
        if lat > -26:
            return "NT"
        return "SA"
    if lng < 141:
        if lat > -26:
            return "QLD"
        if lat > -34:
            return "NSW"
        # SA/VIC border is ~141°E; in the 138-141 band, SA is above ~-36
        if lat > -36:
            return "SA"
        if lat > -38:
            return "VIC"
        return "SA"
    if lng < 150:
        if lat > -29:
            return "QLD"
        if lat > -37:
            return "NSW"
        return "VIC"
    if lng < 154:
        if lat > -29:
            return "QLD"
        if lat > -37.5:
            return "NSW"
        return "VIC"
    return "QLD"


# ---------------------------------------------------------------------------
# Distance calculation
# ---------------------------------------------------------------------------


_AU_STATE_CODES = {"NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT"}

import re

_ADDR_RE = re.compile(
    r",?\s+([A-Z][A-Za-z\s]+?)\s+(NSW|VIC|QLD|SA|WA|TAS|NT|ACT)\s+(\d{4})\s*$"
)


def _parse_au_address(address: str) -> tuple[str, str, str]:
    """Extract (suburb, state, postcode) from an Australian address string.

    Handles formats like:
      '7 Raby Rd, LEPPINGTON NSW 2179'
      '1 DUNN RD, SMEATON GRANGE NSW 2567'
    Returns ('', '', '') if it can't parse.
    """
    m = _ADDR_RE.search(address)
    if m:
        suburb = m.group(1).strip().title()
        return suburb, m.group(2), m.group(3)
    return "", "", ""


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
# Adapters
# ---------------------------------------------------------------------------


async def fetch_fuelwatch(
    client: "httpx.AsyncClient", location: Location, radius_km: float
) -> list[Station]:
    """FuelWatch WA — official government JSON API, no auth needed."""
    stations = []
    # FuelWatch uses short codes: ULP, PUP (PULP), DSL, BDL (Brand Diesel), LPG, 98R, E85
    fuel_types_map = {
        "ULP": "U91",
        "PUP": "U95",
        "DSL": "DSL",
        "BDL": "PDSL",
        "LPG": "LPG",
        "98R": "U98",
        "E85": "E10",
    }

    async def _fetch_type(fw_code: str, our_code: str) -> list[Station]:
        try:
            resp = await client.get(
                "https://www.fuelwatch.wa.gov.au/api/sites",
                params={"fuelType": fw_code},
                timeout=15,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            results = []
            for site in data:
                addr = site.get("address", {})
                product = site.get("product", {})
                lat = float(addr.get("latitude", 0))
                lng = float(addr.get("longitude", 0))
                dist = haversine_km(location.lat, location.lng, lat, lng)
                if dist > radius_km:
                    continue
                price_today = product.get("priceToday")
                price_tomorrow = product.get("priceTomorrow")
                results.append(
                    Station(
                        name=site.get("siteName", "Unknown"),
                        brand=site.get("brandName", ""),
                        address=addr.get("line1", ""),
                        suburb=addr.get("location", ""),
                        state="WA",
                        postcode=str(addr.get("postCode", "")),
                        lat=lat,
                        lng=lng,
                        prices={our_code: round(float(price_today) / 100, 3) if price_today else None},
                        updated_at="",
                        source="FuelWatch WA (govt)",
                        distance_km=round(dist, 1),
                        price_tomorrow=(
                            {our_code: round(float(price_tomorrow) / 100, 3)}
                            if price_tomorrow
                            else None
                        ),
                    )
                )
            return results
        except Exception:
            return []

    # Fetch all fuel types concurrently
    type_results = await asyncio.gather(
        *[_fetch_type(fw_code, our_code) for fw_code, our_code in fuel_types_map.items()]
    )

    # Merge stations by (name, address) — combine fuel type prices
    merged: dict[str, Station] = {}
    for result_list in type_results:
        for s in result_list:
            key = f"{s.name}|{s.address}"
            if key in merged:
                merged[key].prices.update(s.prices)
                if s.price_tomorrow:
                    if merged[key].price_tomorrow is None:
                        merged[key].price_tomorrow = {}
                    merged[key].price_tomorrow.update(s.price_tomorrow)
            else:
                merged[key] = s
    return list(merged.values())


async def fetch_fuelcheck(
    client: "httpx.AsyncClient", location: Location, radius_km: float
) -> list[Station]:
    """NSW FuelCheck official govt API — NSW, ACT, TAS. Requires env vars."""
    creds = _get_credentials()
    consumer_key = creds.get("fuelcheck_key", "")
    consumer_secret = creds.get("fuelcheck_secret", "")
    if not consumer_key or not consumer_secret:
        return []

    import base64

    # Step 1: Get OAuth2 access token
    credentials = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode()).decode()
    try:
        token_resp = await client.get(
            "https://api.onegov.nsw.gov.au/oauth/client_credential/accesstoken",
            params={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {credentials}"},
            timeout=10,
        )
        if token_resp.status_code != 200:
            print(f"FuelCheck token error: {token_resp.status_code}", file=sys.stderr)
            return []
        access_token = token_resp.json().get("access_token", "")
        if not access_token:
            return []
    except Exception as e:
        print(f"FuelCheck token exception: {e}", file=sys.stderr)
        return []

    # Step 2: Fetch nearby prices for each fuel type in parallel
    # API requires one fuel type per request
    fuel_types_to_query = ["E10", "U91", "P95", "P98", "DL", "PDL", "LPG"]
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "apikey": consumer_key,
        "transactionid": str(int(time.time())),
        "requesttimestamp": time.strftime("%d/%m/%Y %I:%M:%S %p"),
    }
    base_body = {
        "latitude": str(location.lat),
        "longitude": str(location.lng),
        "radius": str(int(radius_km)),
        "sortby": "price",
        "sortascending": "true",
    }

    async def _fetch_one(fueltype: str) -> dict:
        try:
            resp = await client.post(
                "https://api.onegov.nsw.gov.au/FuelPriceCheck/v2/fuel/prices/nearby",
                headers=headers,
                json={**base_body, "fueltype": fueltype},
                timeout=15,
            )
            return resp.json() if resp.status_code == 200 else {}
        except Exception:
            return {}

    results = await asyncio.gather(*[_fetch_one(ft) for ft in fuel_types_to_query])

    # Step 3: Merge all responses — collect stations and prices across fuel types
    station_lookup: dict[str, dict] = {}
    prices_by_station: dict[str, dict[str, float]] = {}
    updated_by_station: dict[str, str] = {}

    for data in results:
        for s in data.get("stations", []):
            code = str(s.get("code", ""))
            if code and code not in station_lookup:
                station_lookup[code] = s

        for p in data.get("prices", []):
            scode = str(p.get("stationcode", ""))
            fueltype = p.get("fueltype", "")
            price_val = p.get("price")
            if not scode or not fueltype or price_val is None:
                continue
            our_code = FUELCHECK_FUEL_MAP.get(fueltype)
            if our_code is None:
                continue
            # FuelCheck prices are in cents/L
            prices_by_station.setdefault(scode, {})[our_code] = round(float(price_val) / 100, 3)
            lastupdated = p.get("lastupdated", "")
            if lastupdated:
                updated_by_station[scode] = lastupdated

    # Step 4: Build Station objects
    stations = []
    for scode, price_map in prices_by_station.items():
        sinfo = station_lookup.get(scode, {})
        lat = float(sinfo.get("location", {}).get("latitude", 0))
        lng = float(sinfo.get("location", {}).get("longitude", 0))
        if lat == 0 and lng == 0:
            continue
        dist = haversine_km(location.lat, location.lng, lat, lng)
        if dist > radius_km:
            continue

        stations.append(
            Station(
                name=sinfo.get("name", "Unknown"),
                brand=sinfo.get("brand", ""),
                address=sinfo.get("address", ""),
                suburb=sinfo.get("suburb", ""),
                state=sinfo.get("state", "NSW"),
                postcode=sinfo.get("postcode", ""),
                lat=lat,
                lng=lng,
                prices=price_map,
                updated_at=updated_by_station.get(scode, ""),
                source="FuelCheck",
                distance_km=round(dist, 1),
            )
        )
    print(f"FuelCheck returned {len(stations)} stations", file=sys.stderr)
    return stations


async def fetch_fuelsnoop(
    client: "httpx.AsyncClient", location: Location, radius_km: float
) -> list[Station]:
    """FuelSnoop via Supabase — NSW/QLD, embedded anon key, no registration."""
    # Convert radius to a bounding box
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * math.cos(math.radians(location.lat)))

    anon_key = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImpxZHl2dGhwdmdudmxvamVmcGF2Iiwi"
        "cm9sZSI6ImFub24iLCJpYXQiOjE3MDEwODM2MzksImV4cCI6MjAxNjY1OTYzOX0."
        "7fEHEq5g3OFLBSyzuOObdJLZNlqFyVJPoYre2fYzN0E"
    )

    try:
        resp = await client.post(
            "https://jqdyvthpvgnvlojefpav.supabase.co/rest/v1/rpc/sites_in_view",
            headers={
                "apikey": anon_key,
                "authorization": f"Bearer {anon_key}",
                "content-profile": "public",
                "Referer": "https://www.fuelsnoop.com.au/",
            },
            json={
                "min_lng": location.lng - dlng,
                "min_lat": location.lat - dlat,
                "max_lng": location.lng + dlng,
                "max_lat": location.lat + dlat,
                "brand_names": [],
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []

    stations = []
    for site in data:
        lat = float(site.get("lat", 0))
        lng = float(site.get("lng", 0))
        dist = haversine_km(location.lat, location.lng, lat, lng)
        if dist > radius_km:
            continue

        prices = {}
        prices_raw = site.get("prices", {})
        if isinstance(prices_raw, dict):
            for fuel_key, fuel_data in prices_raw.items():
                code = FUELSNOOP_FUEL_MAP.get(fuel_key, fuel_key)
                if isinstance(fuel_data, dict) and fuel_data.get("price"):
                    raw = float(fuel_data["price"])
                    # FuelSnoop prices are in cents per litre (e.g., 209.9)
                    prices[code] = round(raw / 100, 3)
                elif isinstance(fuel_data, (int, float)):
                    prices[code] = round(float(fuel_data) / 100, 3)

        updated = ""
        for fuel_data in (prices_raw.values() if isinstance(prices_raw, dict) else []):
            if isinstance(fuel_data, dict) and fuel_data.get("api_updated_at"):
                updated = fuel_data["api_updated_at"]
                break

        raw_addr = site.get("address", "")
        suburb = site.get("suburb") or ""
        state = site.get("state") or ""
        postcode = site.get("postcode") or ""
        # FuelSnoop often omits suburb/state — parse from address
        if (not suburb or not state) and raw_addr:
            parsed_sub, parsed_st, parsed_pc = _parse_au_address(raw_addr)
            suburb = suburb or parsed_sub
            state = state or parsed_st
            postcode = postcode or parsed_pc

        stations.append(
            Station(
                name=site.get("site_name", "Unknown"),
                brand=site.get("brand_name", ""),
                address=raw_addr,
                suburb=suburb,
                state=state,
                postcode=postcode,
                lat=lat,
                lng=lng,
                prices=prices,
                updated_at=updated,
                source="FuelSnoop",
                distance_km=round(dist, 1),
            )
        )
    return stations


async def fetch_petrolspy(
    client: "httpx.AsyncClient", location: Location, radius_km: float
) -> list[Station]:
    """PetrolSpy — all of Australia, reverse-engineered public endpoint."""
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * math.cos(math.radians(location.lat)))

    try:
        resp = await client.get(
            "https://petrolspy.com.au/webservice-1/station/box",
            params={
                "neLat": location.lat + dlat,
                "neLng": location.lng + dlng,
                "swLat": location.lat - dlat,
                "swLng": location.lng - dlng,
            },
            headers={
                "Accept": "application/json",
                "Referer": "https://petrolspy.com.au/",
                "x-ps-fp": "06999ae0c2fa02880528b0a549374286",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []

    message = data.get("message", {})
    raw_list = message.get("list", []) if isinstance(message, dict) else []

    stations = []
    for site in raw_list:
        lat = float(site.get("location", {}).get("y", 0))
        lng = float(site.get("location", {}).get("x", 0))
        dist = haversine_km(location.lat, location.lng, lat, lng)
        if dist > radius_km:
            continue

        prices = {}
        for fuel_key, fuel_data in site.get("prices", {}).items():
            code = PETROLSPY_FUEL_MAP.get(fuel_key)
            if code is None:
                continue  # unmapped or explicitly excluded
            if isinstance(fuel_data, dict) and fuel_data.get("amount"):
                raw = float(fuel_data["amount"])
                # PetrolSpy prices are in cents per litre (e.g., 178.9)
                prices[code] = round(raw / 100, 3)
            elif isinstance(fuel_data, (int, float)):
                prices[code] = round(float(fuel_data) / 100, 3)

        suburb = site.get("suburb") or site.get("location", {}).get("suburb", "") or ""
        state = site.get("state") or ""
        postcode = site.get("postcode") or site.get("postCode") or ""
        # PetrolSpy often omits state — derive from coordinates
        if not state:
            state = _state_from_coords(lat, lng)

        stations.append(
            Station(
                name=site.get("name", "Unknown"),
                brand=site.get("brand", ""),
                address=site.get("address", ""),
                suburb=suburb,
                state=state,
                postcode=postcode,
                lat=lat,
                lng=lng,
                prices=prices,
                updated_at=site.get("updated", ""),
                source="PetrolSpy",
                distance_km=round(dist, 1),
            )
        )
    return stations


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

# State -> which adapters to try, in priority order
STATE_ADAPTERS = {
    "WA": [fetch_fuelwatch, fetch_petrolspy],
    "NSW": [fetch_fuelcheck, fetch_fuelsnoop, fetch_petrolspy],
    "ACT": [fetch_fuelcheck, fetch_fuelsnoop, fetch_petrolspy],
    "QLD": [fetch_fuelsnoop, fetch_petrolspy],
    "TAS": [fetch_fuelcheck, fetch_fuelsnoop, fetch_petrolspy],
    "VIC": [fetch_petrolspy],
    "SA": [fetch_petrolspy],
    "NT": [fetch_petrolspy],
}

# States where we merge FuelCheck + FuelSnoop for best coverage + freshness
MERGE_STATES = {"NSW", "ACT", "TAS"}


def _compute_staleness(updated_at: str) -> dict:
    """Parse an ISO timestamp and return staleness info."""
    if not updated_at or not updated_at.strip():
        return {"age_hours": None, "is_stale": False, "age_display": "unknown"}
    try:
        from datetime import datetime, timezone

        # Handle various ISO formats
        ts = updated_at.replace("Z", "+00:00")
        # Try parsing with timezone
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            # Try without timezone
            dt = datetime.fromisoformat(updated_at.split("+")[0].split("Z")[0])
            dt = dt.replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt
        age_hours = delta.total_seconds() / 3600
        is_stale = age_hours > 48

        # Human-friendly display
        if age_hours < 1:
            minutes = int(delta.total_seconds() / 60)
            age_display = f"{max(1, minutes)} min ago"
        elif age_hours < 24:
            age_display = f"{int(age_hours)} hr ago"
        else:
            days = int(age_hours / 24)
            age_display = f"{days} day{'s' if days != 1 else ''} ago"

        return {
            "age_hours": round(age_hours, 1),
            "is_stale": is_stale,
            "age_display": age_display,
        }
    except Exception:
        return {"age_hours": None, "is_stale": False, "age_display": "unknown"}


def _sanitize_prices(prices: dict[str, float | None]) -> dict[str, float | None]:
    """Pure function: filter prices to sane range."""
    return {
        code: price
        for code, price in prices.items()
        if price is not None and MIN_SANE_PRICE <= price <= MAX_SANE_PRICE
    }


def sanitize_station(station: Station) -> Station:
    """Pure function: return station with sanitized prices."""
    return Station(
        **{
            **asdict(station),
            "prices": _sanitize_prices(station.prices),
            "price_tomorrow": (
                _sanitize_prices(station.price_tomorrow)
                if station.price_tomorrow
                else None
            ),
        }
    )


def has_prices(station: Station) -> bool:
    return bool(station.prices)


def has_fuel_type(fuel_type: str) -> Callable[[Station], bool]:
    """Return a predicate that checks if a station has the given fuel type."""
    return lambda s: fuel_type in s.prices and s.prices[fuel_type] is not None


def attach_staleness(station: Station) -> tuple[Station, dict]:
    """Pure function: compute staleness for a station."""
    return station, _compute_staleness(station.updated_at)


def sort_key(sort_fuel: str, staleness_map: dict[int, dict]) -> Callable[[Station], tuple]:
    """Return a sort key function: stale stations last, then by price, then distance."""
    return lambda s: (
        1 if staleness_map.get(id(s), {}).get("is_stale") else 0,
        s.prices.get(sort_fuel) or 999,
        s.distance_km or 999,
    )


def to_dict_with_staleness(staleness_map: dict[int, dict]) -> Callable[[Station], dict]:
    """Return a function that converts a station to a dict with staleness info."""
    def _build(s: Station) -> dict:
        query = quote(f"{s.name}, {s.address}")
        return {
            **asdict(s),
            "google_maps_url": f"https://www.google.com/maps/search/?api=1&query={query}",
            "apple_maps_url": f"https://maps.apple.com/?q={quote(s.name)}&ll={s.lat},{s.lng}",
            "staleness": staleness_map.get(id(s), _compute_staleness("")),
        }
    return _build


def _default_sort_fuel(stations: list[Station]) -> str:
    """Pick the most common fuel type to sort by."""
    counts: dict[str, int] = reduce(
        lambda acc, code: {**acc, code: acc.get(code, 0) + 1},
        (code for s in stations for code, price in s.prices.items() if price is not None),
        {},
    )
    return next(
        (pref for pref in ("U91", "E10", "DSL") if pref in counts),
        max(counts, key=counts.get) if counts else "U91",
    )


def pipe(data, *fns):
    """Thread data through a sequence of functions."""
    return reduce(lambda acc, fn: fn(acc), fns, data)


async def fetch_prices(
    location: Location, radius_km: float, fuel_type: str | None = None
) -> dict:
    """Main entry point: locate, fetch, normalise, return JSON."""

    import httpx

    async with httpx.AsyncClient() as client:
        state = location.state or _state_from_coords(location.lat, location.lng)
        location.state = state

        # Try adapters in priority order until one returns results
        all_stations, source_used = await _fetch_from_adapters(
            client, state, location, radius_km
        )

        # Functional pipeline: sanitize → filter → enrich → sort → cap → serialize
        sanitized = pipe(
            all_stations,
            lambda ss: list(map(sanitize_station, ss)),
            lambda ss: list(filter(has_prices, ss)),
        )

        # Compute staleness as a parallel map, build lookup
        staleness_pairs = list(map(attach_staleness, sanitized))
        staleness_map = {id(s): stal for s, stal in staleness_pairs}
        stale_count = sum(1 for _, stal in staleness_pairs if stal["is_stale"])

        # Filter by fuel type, sort, cap, serialize
        sort_fuel = fuel_type or _default_sort_fuel(sanitized)
        station_dicts = pipe(
            sanitized,
            lambda ss: list(filter(has_fuel_type(fuel_type), ss)) if fuel_type else ss,
            lambda ss: sorted(ss, key=sort_key(sort_fuel, staleness_map)),
            lambda ss: ss[:10],
            lambda ss: list(map(to_dict_with_staleness(staleness_map), ss)),
        )

        result = {
            "location": {
                "city": location.city,
                "state": state,
                "postcode": location.postcode,
                "lat": location.lat,
                "lng": location.lng,
                "method": location.method,
            },
            "query": {
                "radius_km": radius_km,
                "fuel_type": fuel_type,
                "sort_by": sort_fuel,
            },
            "results": {
                "count": len(station_dicts),
                "total_found": len(sanitized),
                "source": source_used,
                "stations": station_dicts,
            },
        }
        if stale_count > 0:
            result["stale_count"] = stale_count
            result["stale_note"] = (
                f"{stale_count} station(s) have prices older than 48 hours "
                "and may not reflect current pricing. They are sorted to the bottom."
            )
        return result


def _merge_stations(primary: list["Station"], secondary: list["Station"]) -> list["Station"]:
    """Merge two station lists. Match by proximity (<150m). Take freshest price per fuel type."""
    from datetime import datetime, timezone

    def _parse_ts(ts: str) -> float:
        """Return unix timestamp, or 0 if unparseable."""
        if not ts or not ts.strip():
            return 0
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(ts.strip(), fmt).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
        return 0

    # Index primary stations by (rounded lat, rounded lng) for fast lookup
    # Use 4 decimal places (~11m precision) for bucketing, check 150m for match
    buckets: dict[tuple[int, int], list["Station"]] = {}
    for s in primary:
        key = (round(s.lat * 1000), round(s.lng * 1000))
        buckets.setdefault(key, []).append(s)

    merged = {f"{s.lat:.5f},{s.lng:.5f}": s for s in primary}

    for s2 in secondary:
        # Search nearby buckets for a match
        bkey = (round(s2.lat * 1000), round(s2.lng * 1000))
        match = None
        for dk_lat in (-1, 0, 1):
            for dk_lng in (-1, 0, 1):
                for candidate in buckets.get((bkey[0] + dk_lat, bkey[1] + dk_lng), []):
                    if haversine_km(s2.lat, s2.lng, candidate.lat, candidate.lng) < 0.15:
                        match = candidate
                        break
                if match:
                    break
            if match:
                break

        if match:
            # Merge: for each fuel type, take the price with the newer timestamp
            mkey = f"{match.lat:.5f},{match.lng:.5f}"
            existing = merged[mkey]
            ts_existing = _parse_ts(existing.updated_at)
            ts_new = _parse_ts(s2.updated_at)

            for fuel, price in s2.prices.items():
                if price is None:
                    continue
                if fuel not in existing.prices or existing.prices[fuel] is None:
                    # New fuel type not in primary — add it
                    existing.prices[fuel] = price
                elif ts_new >= ts_existing:
                    # Secondary has same or newer timestamp — take its price
                    # (>= so government data wins ties)
                    existing.prices[fuel] = price

            # Update timestamp to the newest
            if ts_new >= ts_existing:
                existing.updated_at = s2.updated_at
        else:
            # No match — add as new station
            skey = f"{s2.lat:.5f},{s2.lng:.5f}"
            if skey not in merged:
                merged[skey] = s2

    return list(merged.values())


async def _fetch_from_adapters(client, state, location, radius_km):
    """Fetch from adapters. For merge states, combine FuelCheck + FuelSnoop for best coverage."""

    if state in MERGE_STATES:
        # Try to fetch from both FuelCheck and FuelSnoop in parallel
        fc_result, fs_result = await asyncio.gather(
            _safe_fetch(fetch_fuelcheck, client, location, radius_km),
            _safe_fetch(fetch_fuelsnoop, client, location, radius_km),
        )

        if fc_result and fs_result:
            # Merge: FuelSnoop as base (more stations), FuelCheck overrides where fresher
            merged = _merge_stations(fs_result, fc_result)
            # Tag merged source
            for s in merged:
                s.source = "FuelCheck+FuelSnoop"
            print(f"Merged {len(fc_result)} FuelCheck + {len(fs_result)} FuelSnoop → {len(merged)} stations", file=sys.stderr)
            return merged, "FuelCheck+FuelSnoop"
        elif fc_result:
            return fc_result, "FuelCheck"
        elif fs_result:
            return fs_result, "FuelSnoop"
        # Both failed — fall through to PetrolSpy

    # Standard fallback chain
    adapters = STATE_ADAPTERS.get(state, [fetch_petrolspy])
    # Skip adapters already tried in merge path
    skip = {fetch_fuelcheck, fetch_fuelsnoop} if state in MERGE_STATES else set()
    for adapter in adapters:
        if adapter in skip:
            continue
        try:
            results = await adapter(client, location, radius_km)
            if results:
                return results, results[0].source
        except Exception as e:
            print(f"{adapter.__name__} failed: {e}", file=sys.stderr)
            continue
    return [], ""


async def _safe_fetch(adapter, client, location, radius_km) -> list:
    """Call an adapter, returning [] on any failure."""
    try:
        return await adapter(client, location, radius_km)
    except Exception as e:
        print(f"{adapter.__name__} failed: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find cheapest fuel prices nearby (Australia)")
    parser.add_argument("--location", "-l", help="Suburb or city name (e.g., 'Newtown NSW')")
    parser.add_argument("--postcode", "-p", type=int, help="Australian postcode (e.g., 2042)")
    parser.add_argument("--lat", type=float, help="Latitude")
    parser.add_argument("--lng", type=float, help="Longitude")
    parser.add_argument(
        "--fuel-type",
        "-f",
        choices=["E10", "U91", "U95", "U98", "DSL", "PDSL", "LPG"],
        help="Filter to a specific fuel type",
    )
    parser.add_argument(
        "--radius", "-r", type=float, default=5.0, help="Search radius in km (default: 5)"
    )
    parser.add_argument("--no-cache", action="store_true", help="Skip cache")
    return parser.parse_args()


async def main() -> None:
    import httpx

    args = parse_args()

    # Check cache — include credentials presence so adding/removing keys invalidates cache
    has_creds = "1" if _get_credentials().get("fuelcheck_key") else "0"
    cache_key = f"prices_{args.location}_{args.postcode}_{args.lat}_{args.lng}_{args.fuel_type}_{args.radius}_fc{has_creds}"
    if args.no_cache:
        # Clear both location and price cache
        loc_cache = CACHE_DIR / "location.json"
        if loc_cache.exists():
            loc_cache.unlink()
        price_cache = _cache_path(cache_key)
        if price_cache.exists():
            price_cache.unlink()
    else:
        cached = cache_get(cache_key)
        if cached:
            print(json.dumps(cached, indent=2))
            return

    async with httpx.AsyncClient() as client:
        location = await location_from_args(args, client)

    if not location:
        print(json.dumps({"error": "Could not determine location. Use --location or --lat/--lng."}))
        sys.exit(1)

    if location.country and location.country not in ("Australia", "AU"):
        print(
            json.dumps(
                {
                    "error": f"Detected location in {location.country}. This tool currently supports Australia only.",
                    "detected": {
                        "city": location.city,
                        "country": location.country,
                        "lat": location.lat,
                        "lng": location.lng,
                    },
                }
            )
        )
        sys.exit(1)

    # Flag IP-only detection so the agent knows accuracy is limited
    if location.method in ("ip-api.com", "ip-fallback") and not (args.lat and args.lng):
        location_confidence = "low"
    else:
        location_confidence = "high"

    result = await fetch_prices(location, args.radius, args.fuel_type)

    # Add location confidence to output so the agent knows whether to verify
    result["location"]["confidence"] = location_confidence
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
