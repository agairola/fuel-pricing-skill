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

Find the cheapest fuel at nearby stations across Australia.

## Philosophy: zero friction

No installs, no API keys, no config. Everything is handled silently.

- **uv** is needed. If missing: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **API keys** not needed. Free public APIs. Optional: `FUELCHECK_CONSUMER_KEY` for official NSW govt data.
- **Dependencies** declared inline (PEP 723) — `uv run` installs them automatically.

## Location detection — fallback chain

The script resolves location with massive redundancy. Each level falls back to the next:

1. **App / chat platform** — If the user shared location via Telegram, WhatsApp, Signal, Discord, etc., pass the lat/lng directly with `--lat`/`--lng`. If they typed a place name or postcode, use `--location` or `--postcode`. These go through Nominatim geocoding with IP fallback.
2. **Browser consent flow** — When no location args are given (auto-detect), the script opens a localhost page requesting `navigator.geolocation` (WiFi triangulation, ~15-50ft accuracy). Cached 24hrs. Same pattern as `gh auth login`.
3. **IP geolocation** — Final fallback via ip-api.com. City-level only, often inaccurate for non-city users.

If all three fail, ask the user for their suburb or postcode.

## Setup status

!`command -v uv > /dev/null 2>&1 && echo "uv: installed" || echo "uv: NOT INSTALLED"`

## Workflow

### Step 0: Ensure uv is available

If not installed: `curl -LsSf https://astral.sh/uv/install.sh | sh`

### Step 1: Get prices

```bash
# Best: user shared location via chat platform
uv run "${CLAUDE_SKILL_DIR}/scripts/fuel_prices.py" --lat -34.07 --lng 150.74

# User mentioned a place name or postcode
uv run "${CLAUDE_SKILL_DIR}/scripts/fuel_prices.py" --location "Newtown, NSW"
uv run "${CLAUDE_SKILL_DIR}/scripts/fuel_prices.py" --postcode 2042

# Auto-detect (terminal — opens browser on first run)
uv run "${CLAUDE_SKILL_DIR}/scripts/fuel_prices.py"

# Options: --fuel-type E10|U91|U95|U98|DSL|PDSL|LPG  --radius 10  --no-cache
```

Stderr has diagnostics. Only parse stdout (JSON).

### Step 2: Present results

```
Cheapest [fuel type]: $[price]/L at [Station] ([distance] away, updated [freshness])

| Station | [fuel types...] | Distance | Updated |
|---------|----------------|----------|---------|
| **[cheapest]** | **$X.XX** | X.X km | X min ago |
| [others] | $X.XX | X.X km | X min ago |

[N] stations within [radius]km of [location] · Source: [source]
```

- Bold the cheapest row. Sort by price ascending (default U91 or E10).
- Use `staleness.age_display` for the Updated column. Flag `is_stale` (>48hrs) with a note.
- Stale stations are auto-sorted to the bottom.
- Tomorrow's prices (WA only): add "Tomorrow: $X.XX".
- Cap at 10 stations.

### Step 3: Edge cases

- **Low confidence** (`confidence: "low"`): IP-only detection. Ask the user: "I detected [city] but that might not be exact. What suburb or postcode are you near?" On chat platforms, suggest they share location via the platform's location button. Rerun with `--location`, `--postcode`, or `--lat`/`--lng`.
- **Stale prices**: `stale_count`/`stale_note` in JSON — mention to user. Stale stations pushed to bottom.
- **Price sanity**: $0.50–$5.00/L range enforced. Out-of-range prices filtered automatically.
- **No results**: Suggest `--radius 10` or a nearby suburb.
- **API errors**: Multiple sources tried per state with auto-fallback. If all fail, suggest `--location`.

## Data sources

| State | Primary | Fallback |
|-------|---------|----------|
| WA | FuelWatch (govt, includes tomorrow's prices) | PetrolSpy |
| NSW, QLD | FuelSnoop | PetrolSpy |
| VIC, SA, TAS, NT, ACT | PetrolSpy | — |

## Fuel types

| Code | Name |
|------|------|
| E10 | Ethanol 10% |
| U91 | Unleaded 91 |
| U95 | Premium 95 |
| U98 | Premium 98 |
| DSL | Diesel |
| LPG | LPG |
