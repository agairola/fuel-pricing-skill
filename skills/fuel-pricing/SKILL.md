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

## Quick Reference

### Location Decision Tree

Pick the **first** matching option — do not prompt the user unnecessarily.

| Priority | User provided | Flag to use | Example |
|----------|--------------|-------------|---------|
| 1 | Lat/lng (shared via chat platform) | `--lat -34.07 --lng 150.74` | Telegram location pin |
| 2 | Suburb, city, or address | `--location "Newtown, NSW"` | "fuel near Newtown" |
| 3 | Postcode | `--postcode 2042` | "fuel near 2042" |
| 4 | Nothing (terminal user) | *(no args — auto-detect)* | "cheapest fuel nearby" |

Auto-detect opens a browser consent page for GPS (cached 24hrs), then falls back to IP geolocation.

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

### Output Format

```
Cheapest [fuel type]: $[price]/L at [Station] ([distance] away, updated [freshness])

| Station | [fuel types...] | Distance | Updated |
|---------|----------------|----------|---------|
| **[cheapest]** | **$X.XX** | X.X km | X min ago |
| [others] | $X.XX | X.X km | X min ago |

[N] stations within [radius]km of [location] · Source: [source]
```

### Formatting Rules

| Rule | Detail |
|------|--------|
| Sort order | Price ascending (cheapest first) |
| Cheapest row | Bold station name and price |
| Updated column | Use `staleness.age_display` from JSON |
| Stale stations | Auto-sorted to bottom; flag with note if `is_stale` is true |
| Tomorrow prices | WA only — append "Tomorrow: $X.XX" when available |
| Max rows | Cap at 10 stations |

## Handling Edge Cases

Listed by priority — handle the first applicable case.

| Priority | Condition | JSON signal | Action |
|----------|-----------|-------------|--------|
| 1 | Low confidence location | `confidence: "low"` | Tell user: "I detected [city] but that might not be exact. What suburb or postcode are you near?" On chat platforms, suggest sharing location via the platform's location button. Rerun with explicit location. |
| 2 | Stale prices | `stale_count > 0`, `stale_note` | Mention staleness to user. Stale stations already pushed to bottom. |
| 3 | No results | empty stations array | Suggest `--radius 10` or ask for a nearby suburb. |
| 4 | API errors | error in JSON | Multiple sources auto-fallback per state. If all fail, suggest `--location` with explicit suburb. |

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

### Location Fallback Chain (internal)

The script resolves location automatically with this chain:

1. **Explicit args** — `--lat`/`--lng`, `--location`, or `--postcode` (Nominatim geocoding)
2. **Browser consent** — localhost page requesting `navigator.geolocation` (WiFi, ~15-50ft accuracy, cached 24hrs)
3. **IP geolocation** — ip-api.com (city-level only, often inaccurate for non-city users)
