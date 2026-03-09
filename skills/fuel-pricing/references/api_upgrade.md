# Upgrading to Official Government APIs

The skill works out of the box using free public sources (FuelSnoop, PetrolSpy, FuelWatch). For more reliable, government-backed data in specific states, you can register for official APIs.

## NSW FuelCheck API (covers NSW + ACT + Tasmania)

The best fuel price API in Australia. Provides native radius search from lat/lng, all fuel types, real-time updates.

### How to register (free, takes ~2 minutes)

1. Go to https://api.nsw.gov.au
2. Click "Sign Up" and create an account
3. Navigate to "Products" and subscribe to "Fuel API"
4. You'll receive a **Consumer Key** and **Consumer Secret**

### Configure the skill

Add to your shell profile (`~/.zshrc` or `~/.bashrc`):

```bash
export FUELCHECK_CONSUMER_KEY=your-consumer-key
export FUELCHECK_CONSUMER_SECRET=your-consumer-secret
```

Then restart your terminal or run `source ~/.zshrc`.

The skill will automatically detect the key and use the official API for NSW, ACT, and Tasmania.

### What you get

- **Rate limit:** 2,500 calls/month (free tier), 5 calls/minute
- **Data:** Real-time prices from government-mandated reporting
- **Features:** Native radius search by lat/lng, suburb/postcode filtering
- **Coverage:** NSW, ACT, Tasmania

## VIC Servo Saver API

### How to register

1. Email fuel.program@service.vic.gov.au to request API access
2. You'll receive an API Consumer ID after approval

### Important caveat

Victoria's data has a **24-hour delay** — prices shown are from yesterday. This is a government policy decision, not a technical limitation.

## QLD and SA (Informed Sources)

Queensland and South Australia use Informed Sources as their government-appointed data aggregator. Registration is available for app developers.

1. Visit https://informedsources.com
2. Contact them about API access for your application

## WA FuelWatch

No registration needed — the FuelWatch JSON API is completely open. The skill already uses it as the primary source for Western Australia.
