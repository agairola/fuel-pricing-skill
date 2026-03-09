---
name: fuel-pricing
description: >-
  Find the cheapest fuel prices near the user's current location in Australia.
  Use this skill whenever the user asks about fuel prices, petrol prices, gas
  station prices, servo prices, cheapest fuel, diesel prices, E10 prices, or
  wants to compare fuel costs nearby. Also trigger when the user mentions filling
  up, refueling, or asks "where should I get fuel/petrol/diesel". Works across
  all Australian states with zero configuration — no API keys needed. Works in
  any environment — Telegram, WhatsApp, Signal, Discord, terminal, or any chat
  platform.
---

# Fuel Pricing Skill

Find the cheapest fuel at nearby stations across Australia. Zero config — no API keys, no setup.

## When to Use

Trigger this skill when the user:

- Asks about fuel, petrol, diesel, or gas prices
- Wants to compare prices at nearby stations
- Mentions filling up, refueling, or finding a servo
- Asks "where should I get fuel/petrol/diesel?"
- Mentions a specific fuel type (E10, U91, U95, U98, diesel, LPG)

## Prerequisites

- **uv** — `brew install uv` (macOS) or `pip install uv` (all platforms)
- **API keys** — not needed. Optional: `FUELCHECK_CONSUMER_KEY` for official NSW govt data.
- **Dependencies** — declared inline (PEP 723), installed automatically by `uv run`.

## Setup Status

!`command -v uv > /dev/null 2>&1 && echo "uv: installed" || echo "uv: NOT INSTALLED"`

## Location Flow (IMPORTANT — follow this exactly)

Before fetching prices, you MUST resolve the user's location. Follow these steps in order — do NOT skip ahead to IP fallback.

**Step 1: Check what the user already provided.**
- User shared a location pin (Telegram, WhatsApp, Signal, Discord)? Extract lat/lng → use `--lat` / `--lng`. Done.
- User mentioned a suburb, city, or address? → use `--location`. Done.
- User mentioned a postcode? → use `--postcode`. Done.

**Step 2: User said "near me" or "nearby" but gave no location.**
Ask them to share location. Tailor the ask to their platform:
- Telegram: "Tap the paperclip icon → Location → Send My Current Location"
- WhatsApp: "Tap the + button → Location → Send Your Current Location"
- Signal: "Tap the + button → Location"
- Discord/terminal: "What suburb or postcode are you near?"

Wait for their response. Do not proceed without it.

**Step 3: User can't or won't share location.**
Ask: "No worries — what suburb or postcode are you near?" Wait for response.

**Step 4: User refuses to give any location info.**
Only now fall back to auto-detect (no location args). This uses IP geolocation which is city-level only and often wrong. If the result comes back with `confidence: "low"`, tell the user: "I got an approximate location of [city] from your IP but it may not be accurate. Can you tell me your suburb or postcode for better results?"

**Never silently use IP geolocation when you can ask the user instead.**

### Command Template

```bash
uv run "${CLAUDE_SKILL_DIR}/scripts/fuel_prices.py" [LOCATION_FLAGS] [OPTIONS]
```

### Options

| Flag | Values | Default | Purpose |
|------|--------|---------|---------|
| `--fuel-type` | `E10` `U91` `U95` `U98` `DSL` `PDSL` `LPG` | `U91` | Fuel type to search |
| `--radius` | km (integer) | `5` | Search radius |
| `--no-cache` | *(flag)* | off | Force fresh data |

Only parse **stdout** (JSON). Stderr contains diagnostics only.

### Common Commands

```bash
# User shared location via chat platform
uv run "${CLAUDE_SKILL_DIR}/scripts/fuel_prices.py" --lat -34.07 --lng 150.74

# User mentioned a place or postcode
uv run "${CLAUDE_SKILL_DIR}/scripts/fuel_prices.py" --location "Newtown, NSW"
uv run "${CLAUDE_SKILL_DIR}/scripts/fuel_prices.py" --postcode 2042

# Auto-detect location (terminal — opens browser on first run)
uv run "${CLAUDE_SKILL_DIR}/scripts/fuel_prices.py"

# Specific fuel type + wider radius
uv run "${CLAUDE_SKILL_DIR}/scripts/fuel_prices.py" --location "Parramatta" --fuel-type E10 --radius 10
```

## Presenting Results

DO NOT use markdown tables. They don't render on mobile chat platforms (Telegram, WhatsApp, Signal). Use plain text with line breaks instead.

### Output Format

```
Cheapest [fuel type]: $[price]/L
[Station name] · [distance] km · [freshness]

Nearby stations:
1. [Station] — $[price]/L · [distance] km · [freshness]
2. [Station] — $[price]/L · [distance] km · [freshness]
3. [Station] — $[price]/L · [distance] km · [freshness]

[N] stations within [radius]km of [location] · [source]
```

### Example

```
Cheapest U91: $2.17/L
Ampol Smeaton Grange · 4.4 km · 6 hr ago

Nearby stations:
1. EG Ampol Oran Park — $2.19/L · 0.6 km · 6 days ago
2. BP Bringelly — $2.19/L · 1.4 km · 3 days ago
3. 7-Eleven Gregory Hills — $2.19/L · 3.7 km · 6 days ago
4. Ampol Foodary Narellan — $2.19/L · 5.0 km · 6 days ago

9 stations within 5km of Oran Park · FuelSnoop
```

### Formatting Rules

- Sort by price ascending (cheapest first)
- Highlight the cheapest station at the top, separated from the numbered list
- Use `staleness.age_display` from JSON for freshness
- Stale stations (>48hrs): still show them but append a note at the bottom — "Some prices may be outdated"
- WA tomorrow prices: add "Tomorrow: $X.XX" under the station
- Cap at 10 stations
- If user asked about a specific fuel type, show only that type
- If no fuel type specified, default to U91 or E10

## Handling Edge Cases

- **Low confidence** (`confidence: "low"`): Do not silently show results. Tell the user: "I got an approximate location of [city] but it may not be exact. What suburb or postcode are you near?" Rerun with their answer.
- **Stale prices** (`stale_count > 0`): Show results but add a note — "Heads up: some of these prices are a few days old and may have changed."
- **No results** (empty stations): "No stations found within [radius]km. Want me to try a wider search or a different suburb?"
- **API errors**: Multiple sources auto-fallback per state. If all fail: "Couldn't get prices right now. Can you try with a specific suburb name?"

Price sanity ($0.50–$5.00/L) is enforced automatically — out-of-range prices are filtered by the script.

## Reference

### Fuel Types

| Code | Name |
|------|------|
| E10 | Ethanol 10% |
| U91 | Unleaded 91 |
| U95 | Premium 95 |
| U98 | Premium 98 |
| DSL | Diesel |
| LPG | LPG |

### Data Sources

| State | Primary | Fallback |
|-------|---------|----------|
| WA | FuelWatch (govt, includes tomorrow's prices) | PetrolSpy |
| NSW, QLD | FuelSnoop | PetrolSpy |
| VIC, SA, TAS, NT, ACT | PetrolSpy | — |

All data sources are read-only public APIs. FuelWatch is official Australian government open data.
FuelSnoop and PetrolSpy are community data aggregators. No user data is sent to any service
beyond coordinates for the search area.

### Script Location Fallback (internal — for reference only)

When the script runs, it resolves location internally in this order:

1. **Explicit args** — `--lat`/`--lng`, `--location`, or `--postcode` (Nominatim geocoding)
2. **Browser consent** — localhost page requesting `navigator.geolocation` (WiFi, ~15-50ft accuracy, cached 24hrs)
3. **IP geolocation** — ip-api.com (city-level only, often inaccurate for non-city users)

The agent should almost never reach step 3. The Location Flow above ensures the user provides location info before the script runs.
