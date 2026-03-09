# fuel-pricing

Find the cheapest fuel prices near you across Australia. Works in any chat platform — Telegram, WhatsApp, Signal, Discord, terminal.

## Install

```bash
npx skills add agairola/fuel-pricing-skill --skill fuel-pricing
```

## Features

- Zero config — no API keys, no installs beyond `uv`
- Automatic location detection via browser geolocation (cached 24hrs)
- Covers all Australian states (WA, NSW, QLD, VIC, SA, TAS, NT, ACT)
- Multiple fuel types: E10, U91, U95, U98, Diesel, LPG
- Staleness filtering and price sanity checks
- Tomorrow's prices for WA (FuelWatch)

## Data Sources

| State | Primary | Fallback |
|-------|---------|----------|
| WA | FuelWatch (govt) | PetrolSpy |
| NSW, QLD | FuelSnoop | PetrolSpy |
| VIC, SA, TAS, NT, ACT | PetrolSpy | — |

## License

MIT
