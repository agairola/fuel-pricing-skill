---
name: sydney-traffic
description: >-
  Check live traffic incidents, roadworks, and hazards in Sydney.
  Use this skill when the user asks about traffic jams, road closures,
  accidents, roadworks, traffic conditions, how the traffic is, M5 traffic,
  highway conditions, or any Sydney traffic question. Works without API keys
  (provides Live Traffic NSW/Google Maps links) but best with a free TfNSW API key
  for real-time incident data.
---

# Sydney Traffic Skill

Check live traffic incidents, roadworks, and hazards in Sydney. Works without API keys — best with a free TfNSW key for real-time incident data.

## When to Use

Trigger this skill when the user:

- Asks about traffic jams, congestion, or road conditions
- Wants to know about road closures or accidents
- Asks about roadworks or construction on roads
- Mentions a specific Sydney road or highway (M5, M2, Pacific Highway, etc.)
- Says "how's the traffic" or "any traffic on the way to..."
- Asks about fires or floods affecting roads
- Wants to check traffic before a commute or trip

## Prerequisites

- **uv** — `brew install uv` (macOS) or `pip install uv` (all platforms)
- **API keys** — not needed for basic links. Optional: TfNSW API key in `~/.config/sydney-commute/credentials.json` for real-time data.
- **Dependencies** — declared inline (PEP 723), installed automatically by `uv run`.

## Setup Status

!`command -v uv > /dev/null 2>&1 && echo "uv: installed" || echo "uv: NOT INSTALLED"`
!`test -f ~/.config/sydney-commute/credentials.json && python3 -c "import json; d=json.load(open('$HOME/.config/sydney-commute/credentials.json')); print('TfNSW API: configured' if d.get('tfnsw_api_key') else 'TfNSW API: not configured')" 2>/dev/null || echo "TfNSW API: not configured (zero-config mode — Live Traffic NSW/Google Maps links only)"`

## Location Flow (IMPORTANT — follow this exactly)

Before fetching traffic data, you MUST resolve the user's location. Follow these steps in order — do NOT skip ahead to IP fallback.

**Step 1: Check what the user already provided.**
- User shared a location pin (Telegram, WhatsApp, Signal, Discord)? Extract lat/lng -> use `--lat` / `--lng`. Done.
- User mentioned a suburb, city, or address? -> use `--location`. Done.
- User mentioned a specific road? -> use `--road` with a location. Done.

**Step 2: User said "near me" or "nearby" but gave no location.**
Ask them to share location. Tailor the ask to their platform:
- Telegram: "Tap the paperclip icon -> Location -> Send My Current Location"
- WhatsApp: "Tap the + button -> Location -> Send Your Current Location"
- Signal: "Tap the + button -> Location"
- Discord/terminal: "What suburb or postcode are you near?"

Wait for their response. Do not proceed without it.

**Step 3: User can't or won't share location.**
Ask: "No worries — what suburb or postcode are you near?" Wait for response.

**Step 4: User refuses to give any location info.**
Only now fall back to auto-detect (no location args). This uses IP geolocation which is city-level only and often wrong. If the result comes back with `confidence: "low"`, tell the user: "I got an approximate location of [city] from your IP but it may not be accurate. Can you tell me your suburb or postcode for better results?"

**Never silently use IP geolocation when you can ask the user instead.**

### Command Template

```bash
uv run "${CLAUDE_SKILL_DIR}/scripts/traffic.py" [LOCATION_FLAGS] [OPTIONS]
```

### Options

| Flag | Values | Default | Purpose |
|------|--------|---------|---------|
| `--location` | suburb/city name | — | Find incidents near a place name |
| `--lat` | latitude (float) | — | Latitude for nearby search |
| `--lng` | longitude (float) | — | Longitude for nearby search |
| `--radius` | km (integer) | `10` | Search radius for nearby mode |
| `--type` | `incident` `roadwork` `fire` `flood` `all` | `all` | Hazard type filter |
| `--road` | road name (string) | — | Filter by road name (fuzzy match) |
| `--no-cache` | *(flag)* | off | Force fresh data |

Only parse **stdout** (JSON). Stderr contains diagnostics only.

### Common Commands

```bash
# Nearby incidents (auto-detect location)
uv run "${CLAUDE_SKILL_DIR}/scripts/traffic.py"

# Incidents near a suburb
uv run "${CLAUDE_SKILL_DIR}/scripts/traffic.py" --location "Parramatta NSW"

# User shared location via chat platform
uv run "${CLAUDE_SKILL_DIR}/scripts/traffic.py" --lat -33.87 --lng 151.21

# Only roadworks
uv run "${CLAUDE_SKILL_DIR}/scripts/traffic.py" --location "Sydney CBD" --type roadwork

# Filter by road name
uv run "${CLAUDE_SKILL_DIR}/scripts/traffic.py" --location "Arncliffe" --road "M5"

# Wider search radius
uv run "${CLAUDE_SKILL_DIR}/scripts/traffic.py" --location "Penrith" --radius 20

# Zero-config (no API key) — returns Live Traffic NSW / Google Maps links
uv run "${CLAUDE_SKILL_DIR}/scripts/traffic.py" --location "Sydney"
```

## Presenting Results

DO NOT use markdown tables. They don't render on mobile chat platforms (Telegram, WhatsApp, Signal). Use plain text with line breaks instead.

### With API Key: Hazard List

For platforms with hyperlinks (Telegram, Discord, terminal):
```
Traffic near Parramatta (within 10 km):

1. Crash on M4 Western Motorway — 2.3 km away
   Allow extra travel time. Use alternative route.
   [Live Traffic NSW](https://www.livetraffic.com/)

2. Roadwork on Church St — 3.1 km away
   Expect delays. Reduced speed limit in place.
   [Live Traffic NSW](https://www.livetraffic.com/)

3. Flooding on James Ruse Dr — 5.7 km away
   Road closed. Avoid the area.
   [Live Traffic NSW](https://www.livetraffic.com/)

3 incidents found within 10 km
```

For platforms without hyperlinks (WhatsApp, Signal, SMS):
```
Traffic near Parramatta (within 10 km):

1. Crash on M4 Western Motorway — 2.3 km away
   Allow extra travel time. Use alternative route.
   Live Traffic NSW: https://www.livetraffic.com/

2. Roadwork on Church St — 3.1 km away
   Expect delays. Reduced speed limit in place.

3. Flooding on James Ruse Dr — 5.7 km away
   Road closed. Avoid the area.

3 incidents found within 10 km
```

### Zero-Config Mode

When no API key is configured, show the fallback URLs as clickable links.

```
I don't have a TfNSW API key configured, so I can't get real-time incident data. Here are links to check traffic:

Live Traffic NSW: [Live Traffic](https://www.livetraffic.com/)
Google Maps (traffic layer): [View Traffic](google_maps_traffic_url)

For real-time traffic incidents and hazard data, you can set up a free TfNSW API key in about 2 minutes. Want me to walk you through it?
```

### Formatting Rules

- Show hazards sorted by distance (nearest first)
- Include headline, road names, advice, and distance
- For road-filtered results, mention which road was filtered
- No results: "No traffic incidents found within [radius] km. Roads look clear!"
- Google Maps traffic link always available (shows traffic layer)
- Live Traffic NSW link for detailed info

## Handling Edge Cases

- **No API key**: Return zero-config fallback with Live Traffic NSW / Google Maps URLs. Always works.
- **Invalid API key** (`401`): Show error with registration URL. "Your TfNSW API key appears to be invalid. You can get a new one at opendata.transport.nsw.gov.au"
- **No results** (empty hazards): "No traffic incidents found within [radius] km of [location]. Roads look clear! You can also check Live Traffic NSW for the latest updates."
- **Low confidence location** (`confidence: "low"`): Do not silently show results. Tell the user: "I got an approximate location of [city] but it may not be exact. What suburb or postcode are you near?" Rerun with their answer.
- **API errors**: "Couldn't get traffic data right now. The TfNSW API may be temporarily unavailable — try again in a few minutes. In the meantime, check Live Traffic NSW: https://www.livetraffic.com/"

## Upgrade Nudge

In zero-config mode (no API key), always append an upgrade suggestion. This is shown automatically since the API key is required for real-time incident data.

**When to show:** Every time the JSON response has `"api_key_configured": false`.

**What to say (adapt to tone, don't copy verbatim):**

For platforms with hyperlinks:
```
Tip: For real-time traffic incidents and hazard alerts, you can register for a free TfNSW API key (~2 minutes):

1. Sign up at [opendata.transport.nsw.gov.au](https://opendata.transport.nsw.gov.au) — name + email
2. Create an application (any name, e.g. "Traffic Helper")
3. Subscribe to "Traffic" APIs (free)
4. Copy your API key and share it with me — I'll save it securely.
```

For platforms without hyperlinks:
```
Tip: For real-time traffic data, register for a free TfNSW API key (~2 minutes):

1. Sign up at opendata.transport.nsw.gov.au
2. Create an application (any name works)
3. Subscribe to "Traffic" APIs (free tier)
4. Copy your API key and share it with me.
```

**When the user provides a key**, save it securely (same file as sydney-commute):

```bash
mkdir -p ~/.config/sydney-commute
cat > ~/.config/sydney-commute/credentials.json << 'CREDS'
{
  "tfnsw_api_key": "<key>"
}
CREDS
chmod 600 ~/.config/sydney-commute/credentials.json
```

Then confirm: "Key saved securely. Future traffic queries will use real-time TfNSW data."

**Do NOT show the nudge if:**
- TfNSW API is already configured (check setup status above)
- You've already shown the nudge in this conversation

## Reference

### Hazard Types

| Type | Description |
|------|-------------|
| incident | Crashes, breakdowns, lane closures |
| roadwork | Planned and active roadworks |
| fire | Bushfires and grass fires affecting roads |
| flood | Flooding and water over roads |

### Data Source

All data comes from the NSW Government Transport for NSW Live Traffic API. Requires a free TfNSW API key for real-time data. Without a key, fallback links to Live Traffic NSW and Google Maps traffic layer are provided. No user data is sent to any service beyond coordinates for geocoding.

### Script Location Fallback (internal — for reference only)

When the script runs, it resolves location internally in this order:

1. **Explicit args** — `--lat`/`--lng` or `--location` (Nominatim geocoding)
2. **Browser consent** — localhost page requesting `navigator.geolocation` (WiFi, ~15-50ft accuracy, cached 24hrs)
3. **IP geolocation** — ip-api.com (city-level only, often inaccurate for non-city users)

The agent should almost never reach step 3. The Location Flow above ensures the user provides location info before the script runs.
