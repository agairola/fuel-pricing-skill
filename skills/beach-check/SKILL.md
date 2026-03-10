---
name: beach-check
description: >-
  Check beach water quality and swimming safety at NSW beaches.
  Use this skill when the user asks about beach conditions, water quality,
  safe to swim, Beachwatch results, pollution forecast, ocean swimming,
  beach closures, or mentions specific Sydney/NSW beaches like Bondi,
  Coogee, Manly, Bronte, Maroubra, Cronulla, etc. Works with zero
  configuration — no API keys needed. Works in any environment — Telegram,
  WhatsApp, Signal, Discord, terminal, or any chat platform.
---

# Beach Check Skill

Check water quality and swimming safety at NSW beaches. Zero config — no API keys, no setup.

## When to Use

Trigger this skill when the user:

- Asks about beach water quality or swimming safety
- Wants to know if a beach is safe to swim at
- Mentions Beachwatch, pollution forecast, or beach closures
- Asks about conditions at a specific NSW beach (Bondi, Coogee, Manly, Bronte, etc.)
- Says "is it safe to swim" or "beach conditions"
- Wants to find nearby beaches with good water quality

## Prerequisites

- **uv** — `brew install uv` (macOS) or `pip install uv` (all platforms)
- **API keys** — not needed. Fully zero-config.
- **Dependencies** — declared inline (PEP 723), installed automatically by `uv run`.

## Setup Status

!`command -v uv > /dev/null 2>&1 && echo "uv: installed" || echo "uv: NOT INSTALLED"`

## Location Flow (IMPORTANT — follow this exactly)

Before fetching beach data, you MUST resolve the user's location (unless they used `--beach`). Follow these steps in order — do NOT skip ahead to IP fallback.

**Step 1: Check what the user already provided.**
- User shared a location pin (Telegram, WhatsApp, Signal, Discord)? Extract lat/lng -> use `--lat` / `--lng`. Done.
- User mentioned a suburb, city, or address? -> use `--location`. Done.
- User mentioned a specific beach name? -> use `--beach`. Done.

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
uv run "${CLAUDE_SKILL_DIR}/scripts/beach_check.py" [LOCATION_FLAGS] [OPTIONS]
```

### Options

| Flag | Values | Default | Purpose |
|------|--------|---------|---------|
| `--beach` | beach name (string) | — | Search for a specific beach by name |
| `--location` | suburb/city name | — | Find nearby beaches by place name |
| `--lat` | latitude (float) | — | Latitude for nearby search |
| `--lng` | longitude (float) | — | Longitude for nearby search |
| `--radius` | km (integer) | `10` | Search radius for nearby mode |
| `--no-cache` | *(flag)* | off | Force fresh data |

Only parse **stdout** (JSON). Stderr contains diagnostics only.

### Common Commands

```bash
# Search for a specific beach by name
uv run "${CLAUDE_SKILL_DIR}/scripts/beach_check.py" --beach "Bondi"

# User shared location via chat platform
uv run "${CLAUDE_SKILL_DIR}/scripts/beach_check.py" --lat -33.92 --lng 151.26

# User mentioned a place
uv run "${CLAUDE_SKILL_DIR}/scripts/beach_check.py" --location "Coogee, NSW"

# Auto-detect location (terminal — opens browser on first run)
uv run "${CLAUDE_SKILL_DIR}/scripts/beach_check.py"

# Wider search radius
uv run "${CLAUDE_SKILL_DIR}/scripts/beach_check.py" --location "Manly" --radius 20
```

## Presenting Results

DO NOT use markdown tables. They don't render on mobile chat platforms (Telegram, WhatsApp, Signal). Use plain text with line breaks instead.

### Map Links

Each beach in the JSON includes two URL fields:
- `google_maps_url` — searches by beach name
- `apple_maps_url` — pins exact lat/lng with beach name label

Use hyperlinks (not raw URLs) where the platform supports them:
- **Telegram, Discord, terminal**: Use markdown links — `[Beach Name](url)`
- **WhatsApp, Signal, SMS**: These don't support hyperlinks. Put the link on a separate line.

Provide **both** Google Maps and Apple Maps links so the user can choose.

### Two Response Modes

**Beach search (`--beach`):** Show detailed info for the matched beach plus similar alternatives.

**Nearby (`--location` or `--lat/--lng`):** Show a list of nearby beaches sorted by distance with water quality.

### Beach Search: Single Beach Detail

For platforms with hyperlinks (Telegram, Discord, terminal):
```
Bondi Beach — Water Quality: Good

Pollution forecast: Unlikely
Last tested: 10 Mar 2026

[Google Maps](google_maps_url) · [Apple Maps](apple_maps_url)

Similar beaches:
· North Bondi Beach — Good · 0.5 km
· Bronte Beach — Good · 1.2 km
```

For platforms without hyperlinks (WhatsApp, Signal, SMS):
```
Bondi Beach — Water Quality: Good

Pollution forecast: Unlikely
Last tested: 10 Mar 2026

Google Maps: [google_maps_url]
Apple Maps: [apple_maps_url]

Similar beaches:
· North Bondi Beach — Good · 0.5 km
· Bronte Beach — Good · 1.2 km
```

### Nearby: Beach List

For platforms with hyperlinks:
```
Beaches near Coogee (within 10 km):

1. [Coogee Beach](google_maps_url) — Good · 1.2 km
   Pollution: Unlikely
   [Apple Maps](apple_maps_url)
2. [Maroubra Beach](google_maps_url) — Good · 2.1 km
   Pollution: Unlikely
3. [Bronte Beach](google_maps_url) — Fair · 2.5 km
   Pollution: Possible

5 beaches found within 10 km · Beachwatch NSW
```

For platforms without hyperlinks:
```
Beaches near Coogee (within 10 km):

1. Coogee Beach — Good · 1.2 km
   Pollution: Unlikely
   Google Maps: [google_maps_url]
   Apple Maps: [apple_maps_url]
2. Maroubra Beach — Good · 2.1 km
   Pollution: Unlikely
   Google Maps: [google_maps_url]
3. Bronte Beach — Fair · 2.5 km
   Pollution: Possible
   Google Maps: [google_maps_url]

5 beaches found within 10 km · Beachwatch NSW
```

### Formatting Rules

- Beach search mode: show full detail for matched beach, compact list for alternatives
- Nearby mode: top 10 beaches sorted by distance, show quality + pollution forecast
- Stale data: if `"stale": true` in JSON, append the `stale_note` — "Heads up: water quality data for this beach is [N] days old and may not reflect current conditions."
- Both Google Maps and Apple Maps for the top result; Google Maps only for the rest
- Water quality colour hints (for platforms that support emoji):
  - Good = safe to swim
  - Fair = generally safe, minor risks
  - Poor = swimming not recommended
  - Bad = do not swim
- Invite follow-up: "Ask me about a specific beach for more detail" or "Reply with a beach name for full info"

## Handling Edge Cases

- **Low confidence** (`confidence: "low"`): Do not silently show results. Tell the user: "I got an approximate location of [city] but it may not be exact. What suburb or postcode are you near?" Rerun with their answer.
- **Stale data** (`stale: true`): Show results but add a note — "Heads up: water quality data for some beaches is several days old and may not reflect current conditions."
- **No results** (empty beaches): "No beaches found within [radius]km. Want me to try a wider search or a different area? Note: this tool covers NSW beaches only."
- **API errors**: "Couldn't get beach data right now. The Beachwatch API may be temporarily unavailable — try again in a few minutes."
- **Non-NSW location**: The Beachwatch API only covers NSW. If the user is clearly outside NSW, let them know: "Beach water quality data is currently available for NSW beaches only."

## Reference

### Water Quality Ratings

| Rating | Value | Meaning |
|--------|-------|---------|
| Good | 4 | Safe for swimming — low bacterial levels |
| Fair | 3 | Generally safe — minor pollution risk |
| Poor | 2 | Swimming not recommended — elevated bacteria |
| Bad | 1 | Do not swim — high contamination risk |

### Pollution Forecast

| Forecast | Meaning |
|----------|---------|
| Unlikely | Low chance of pollution — safe conditions expected |
| Possible | Some chance of pollution — check before swimming |
| Likely | High chance of pollution — avoid swimming |
| Forecast not available | No forecast data — use water quality rating as guide |

### Data Source

All data comes from the NSW Government Beachwatch program via their public API. No API key needed. Data is updated regularly by the NSW Office of Environment and Heritage. No user data is sent to any service beyond coordinates for geocoding.

### Script Location Fallback (internal — for reference only)

When the script runs, it resolves location internally in this order:

1. **Explicit args** — `--lat`/`--lng` or `--location` (Nominatim geocoding)
2. **Browser consent** — localhost page requesting `navigator.geolocation` (WiFi, ~15-50ft accuracy, cached 24hrs)
3. **IP geolocation** — ip-api.com (city-level only, often inaccurate for non-city users)

The agent should almost never reach step 3. The Location Flow above ensures the user provides location info before the script runs.
