---
name: sydney-tolls
description: >-
  Check Sydney toll road prices and calculate route toll costs.
  Use this skill when the user asks about toll prices, toll costs,
  how much the toll is, M2 toll, harbour bridge toll, tunnel toll,
  cheapest route, toll calculator, E-Tag, Linkt, or any Sydney toll
  road question. Works with zero configuration — no API keys needed.
---

# Sydney Tolls Skill

Check toll road prices and calculate route toll costs across Sydney's motorway network. Zero config — no API keys, no setup.

## When to Use

Trigger this skill when the user:

- Asks about toll prices or toll costs on Sydney roads
- Mentions a specific toll road (M2, M4, M5, M7, M8, M6, NorthConnex, Eastern Distributor, etc.)
- Asks about Sydney Harbour Bridge or Harbour Tunnel tolls
- Wants to know how much tolls will cost for a trip
- Asks about cheapest route, toll calculator, or total toll cost
- Mentions E-Tag, Linkt, E-Toll, or toll registration
- Asks about peak vs off-peak toll pricing
- Wants to compare toll costs for different vehicle types

## Prerequisites

- **uv** — `brew install uv` (macOS) or `pip install uv` (all platforms)
- **API keys** — not needed. Fully zero-config. Toll data is embedded in the script.
- **Dependencies** — declared inline (PEP 723), installed automatically by `uv run`.

## Setup Status

!`command -v uv > /dev/null 2>&1 && echo "uv: installed" || echo "uv: NOT INSTALLED"`

## Command Template

```bash
uv run "${CLAUDE_SKILL_DIR}/scripts/tolls.py" [OPTIONS]
```

## Options

| Flag | Values | Default | Purpose |
|------|--------|---------|---------|
| `--road` | toll road name (string) | — | Search for a specific toll road by name (fuzzy match) |
| `--from` | place name (string) | — | Origin for route toll calculation |
| `--to` | place name (string) | — | Destination for route toll calculation |
| `--vehicle` | `car` `motorcycle` `heavy` | `car` | Vehicle type |
| `--time` | `peak` `offpeak` `weekend` | *(auto-detect)* | Time period for pricing |
| `--all` | *(flag)* | on (default mode) | List all toll roads |
| `--no-cache` | *(flag)* | off | Force fresh geocoding data |

Only parse **stdout** (JSON). Stderr contains diagnostics only.

## Common Commands

```bash
# List all toll roads with current prices (default)
uv run "${CLAUDE_SKILL_DIR}/scripts/tolls.py"

# Search for a specific toll road
uv run "${CLAUDE_SKILL_DIR}/scripts/tolls.py" --road "M2"

# Harbour Bridge toll
uv run "${CLAUDE_SKILL_DIR}/scripts/tolls.py" --road "Harbour Bridge"

# Calculate route tolls
uv run "${CLAUDE_SKILL_DIR}/scripts/tolls.py" --from "Parramatta" --to "Sydney Airport"

# Route tolls for a motorcycle at peak time
uv run "${CLAUDE_SKILL_DIR}/scripts/tolls.py" --from "Hornsby" --to "Sydney CBD" --vehicle motorcycle --time peak

# Heavy vehicle tolls
uv run "${CLAUDE_SKILL_DIR}/scripts/tolls.py" --road "NorthConnex" --vehicle heavy

# Weekend pricing for all roads
uv run "${CLAUDE_SKILL_DIR}/scripts/tolls.py" --time weekend

# Specific time period
uv run "${CLAUDE_SKILL_DIR}/scripts/tolls.py" --road "M4" --time offpeak
```

## Presenting Results

DO NOT use markdown tables. They don't render on mobile chat platforms (Telegram, WhatsApp, Signal). Use plain text with line breaks instead.

### Single Road Result

```
M2 Hills Motorway — $8.49 (peak, car)

Operator: Transurban
Direction: Both directions
Peak: Mon-Fri 6:30-9:30am, 4-7pm. E-TAG or cash/license plate matching.

All prices (car):
  Peak: $8.49
  Off-peak: $5.77
  Weekend: $5.77

Tip: Get an E-Tag from Linkt (linkt.com.au) or E-Toll (myetoll.com.au) to avoid the ~$0.55 license plate matching fee.
```

### Route Result

```
Tolls from Parramatta to Sydney Airport (peak, car):

1. M4 Motorway (WestConnex) — $9.44
2. M5 East Motorway — $5.67

Total tolls: $15.11

Note: This is an estimate based on the straight-line route. Actual tolls depend on your exact route and entry/exit points.

Tip: Get an E-Tag from Linkt or E-Toll to avoid the ~$0.55 per-trip license plate matching fee.
```

### All Roads List

```
Sydney Toll Roads (peak, car):

1. Sydney Harbour Bridge — $4.00 (southbound only, free northbound)
2. Sydney Harbour Tunnel — $4.00 (southbound only)
3. M2 Hills Motorway — $8.49
4. M4 Motorway (WestConnex) — $9.44 (max, distance-based)
5. M5 East Motorway — $5.67
6. M5 South-West Motorway — $5.30
7. M7 Motorway — $9.15 (max, distance-based)
8. M8 Motorway (WestConnex) — $7.65 (max, distance-based)
9. Eastern Distributor — $8.95 (northbound only)
10. Cross City Tunnel — $6.72 (flat rate)
11. Lane Cove Tunnel — $4.07
12. NorthConnex — $8.95
13. M6 Motorway (Stage 1) — $3.91

Currently showing: peak prices
Tip: Off-peak and weekend prices are cheaper on most toll roads.
```

### Formatting Rules

- Show the dollar amount prominently after the road name
- Include the time period and vehicle type in the summary
- For route calculations, number the toll roads and show a total
- Always mention if a toll is one-direction only
- Note distance-based tolls show the maximum price
- Include the E-Tag/Linkt tip in every response
- For motorcycles on the Harbour Bridge/Tunnel, highlight that it's free

## Handling Edge Cases

- **No matching road**: Show the error with a list of all available toll road names so the user can pick the right one.
- **Incomplete route** (only --from or only --to): Show error asking for both origin and destination.
- **Geocoding failure**: "Could not find that location. Try a more specific place name (e.g., 'Parramatta NSW' instead of just 'Parra')."
- **No tolls on route**: "No toll roads found on the direct route between [from] and [to]. Your trip may be toll-free, or the actual driving route may differ from the straight line — check Google Maps for the exact route."
- **M5 cashback**: When showing M5 East or M5 South-West, always mention the M5 cashback scheme for NSW-registered cars.
- **NorthConnex heavy vehicles**: When showing NorthConnex for heavy vehicles, emphasise that heavy vehicles are banned from Pennant Hills Road and must use NorthConnex.

## Reference

### All Sydney Toll Roads and Price Ranges

Sydney Harbour Bridge — Car: $3.00-$4.00, Motorcycle: FREE, Heavy: $6.00-$8.00
Sydney Harbour Tunnel — Car: $3.00-$4.00, Motorcycle: FREE, Heavy: $6.00-$8.00
M2 Hills Motorway — Car: $5.77-$8.49, Motorcycle: $2.14-$3.14, Heavy: $11.54-$16.98
M4 Motorway (WestConnex) — Car: $6.42-$9.44, Motorcycle: $2.38-$3.49, Heavy: $12.84-$18.88
M5 East Motorway — Car: $3.85-$5.67, Motorcycle: $1.43-$2.10, Heavy: $7.70-$11.34
M5 South-West Motorway — Car: $3.60-$5.30, Motorcycle: $1.33-$1.96, Heavy: $7.20-$10.60
M7 Motorway — Car: $9.15 (flat), Motorcycle: $3.39 (flat), Heavy: $18.30 (flat)
M8 Motorway (WestConnex) — Car: $5.20-$7.65, Motorcycle: $1.92-$2.83, Heavy: $10.40-$15.30
Eastern Distributor — Car: $6.08-$8.95, Motorcycle: $2.25-$3.31, Heavy: $12.16-$17.90
Cross City Tunnel — Car: $6.72 (flat), Motorcycle: $2.49 (flat), Heavy: $13.44 (flat)
Lane Cove Tunnel — Car: $2.77-$4.07, Motorcycle: $1.03-$1.51, Heavy: $5.54-$8.14
NorthConnex — Car: $6.08-$8.95, Motorcycle: $2.25-$3.31, Heavy: $18.24-$26.85
M6 Motorway (Stage 1) — Car: $2.66-$3.91, Motorcycle: $0.98-$1.45, Heavy: $5.32-$7.82

### Peak Hours

Peak pricing applies Monday to Friday, 6:30-9:30am and 4:00-7:00pm. All other weekday times are off-peak. Saturday and Sunday use weekend pricing.

### Exceptions

- Sydney Harbour Bridge and Tunnel: Free for motorcycles at all times
- M7 Motorway: Same price at all times (no peak/off-peak difference)
- Cross City Tunnel: Same price at all times (flat rate)
- M5 East and M5 South-West: NSW-registered cars can claim toll refunds via Service NSW (M5 cashback scheme)
- NorthConnex: Heavy vehicles banned from Pennant Hills Road — must use NorthConnex

### Data Source

Toll prices are embedded in the script based on published rates from NSW Government and toll road operators (Transurban, Transport for NSW, Interlink Roads). Prices are maximum rates — distance-based tolls (M4, M5, M7, M8, M6) may be lower for shorter trips. No user data is sent to any service beyond place names for geocoding (Nominatim/OpenStreetMap) when using --from/--to.
