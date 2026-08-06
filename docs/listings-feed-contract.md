# Category listings feed — contract for the frontend

This is the contract for the **new** per-category, per-city listing datasets
(`homz export feed`), meant for the site's Rent / Sale / PG / Commercial
pages. It is additive: the existing Projects feed
(`{cityKey}{Residential|Commercial}Projects.json`) is unchanged and still
serves the Projects catalogue exactly as before.

## Why a separate feed from Projects

Projects (`ggnResidentialProjects.json`, …) are builder-marketed project
pages — one card can represent many unit configurations (a `priceList`,
multiple `flats`). Individual listings (a specific 3BHK for rent, a specific
resale flat) are flatter: one price, one area, one configuration. Forcing
both into the same shape would mean fake multi-config data for listings, so
this is a new, parallel record shape rather than a reuse of the Projects one.

## Segments

One JSON file per **city × category**, `{cityKey}{Category}Properties.json`:

| City key | Category | Example filename |
|---|---|---|
| `ggn`, `noida`, `gNoida`, `delhi`, `faridabad` | `Sale`, `Rent`, `Pg`, `Commercial` | `ggnSaleProperties.json`, `noidaRentProperties.json` |

City keys are the same ones the Projects feed already uses (`lib/scraping/homzbackend.ts`'s `CITY_KEYS`) — no new city-key concept.

**Category meaning** (decided deliberately, not the only possible reading):

- **Sale** — every for-sale listing, i.e. `listingType` ∈ `sale | resale | new_launch | project`. Resale vs. New Launch is **not** split into separate files — see [Client-side filtering](#client-side-filtering-no-extra-requests) below.
- **Rent** — `listingType == rent`.
- **Pg** — `listingType == pg`.
- **Commercial** — `listingType == commercial` **only**. A residential resale flat that happens to be commercially zoned, or has `isCommercial: true` set some other way, still lands in **Sale**, not here. (`isCommercial` is still present on every record as an extra signal if you want it, it's just not what determines the file it's in.)

Listings with an unrecognized/missing listing type, or with no price, configuration, or amenities at all (nothing to render), are withheld from every file — same "don't publish a blank card" rule the Projects feed already applies.

> ⚠️ **Open item to confirm on your side**: this assumes whatever serves `…/api/data?city={segment}` today serves *any* file present in the feed output directory, not a hardcoded list of segment names. If the route is hardcoded to the existing 10 Projects segments, it needs to be opened up to the new ones too — that route code isn't in this repo.

## Response envelope

Identical to the existing Projects feed envelope — no new concept:

```json
{
  "success": true,
  "city": "ggnSaleProperties",
  "page": 1,
  "limit": 4753,
  "total": 4753,
  "results": [ /* records, see below */ ]
}
```

**`results` always contains every record for that city+category — it is not paginated at 500 like the Projects feed can be.** The whole point of this feed is "fetch once per category tab, filter client-side" (see below); a partial file would silently break that. `limit`/`total` will always be equal here.

## Record shape

```json
{
  "title": "3 BHK Flat for Sale in IREO Skyon",
  "location": "Sector 60, Gurgaon",

  "price": "4.65 Cr",
  "priceValue": 46500000,
  "rentMonthly": null,
  "size": "2045 sq.ft",
  "areaValue": 2045.0,
  "bedrooms": 3,
  "configuration": "3 BHK",

  "propertyType": "apartment",
  "listingType": "resale",
  "isCommercial": false,

  "reraId": "GGM/1001/2020/1",
  "projectStatus": "Ready to Move",
  "possession": "Ready to Move",
  "builderDescription": "IREO",
  "aboutProject": ["Spacious flat.", "Park facing."],

  "amenities": [
    {"category": "Sports", "amenities": ["Swimming Pool"]},
    {"category": "Convenience", "amenities": ["Power Backup"]}
  ],
  "specifications": [{"heading": "Flooring", "value": "Vitrified Tiles"}],
  "images": ["https://img.staticmb.com/x/gallery1.jpg"],
  "interiorImages": ["https://img.staticmb.com/x/apartment-interior-1.jpg"],
  "masterPlan": {},
  "landmarks": {"school": [{"name": "DPS", "distance": "1.2 KM"}]},

  "listingUrl": "https://www.magicbricks.com/propertyDetails/...",
  "updatedAt": "2026-08-04T09:00:00+00:00"
}
```

**Fields that don't exist on the Projects feed and exist here specifically to support client-side filtering:**

| Field | Type | Notes |
|---|---|---|
| `priceValue` | number \| null | Raw sale price, INR. `null` for rent-only or price-on-request records. |
| `rentMonthly` | number \| null | Raw monthly rent, INR. Only set when `listingType == rent`. |
| `areaValue` | number \| null | Raw sqft. |
| `bedrooms` | number \| null | Raw integer. |
| `propertyType` | string | One of: `apartment`, `builder_floor`, `independent_house`, `villa`, `plot`, `penthouse`, `studio`, `office`, `retail_shop`, `showroom`, `warehouse`, `co_working`, `farmhouse`, `serviced_apartment`, `other`. |
| `listingType` | string | One of: `sale`, `resale`, `new_launch`, `project`, `rent`, `pg`, `commercial`. This is how you sub-filter **within** the Sale file (Resale vs. New Launch). |

Everything else (`price`, `size`, `possession`, `amenities`, `images`, …) is a formatted display value, same spirit as the Projects feed.

## Client-side filtering — no extra requests

Fetch the one file for the active category tab + city once. Every filter UI
element (property type checkboxes, Resale/New-Launch toggle within Sale,
bedroom count, price/area range sliders) is then a pure array filter over
the already-fetched `results`, e.g.:

```js
const rentListings = await fetch('/api/data?city=ggnRentProperties').then(r => r.json());

const filtered = rentListings.results.filter(r =>
  (propertyTypeFilter.length === 0 || propertyTypeFilter.includes(r.propertyType)) &&
  (bedroomsFilter == null || r.bedrooms === bedroomsFilter) &&
  (priceMax == null || (r.priceValue ?? r.rentMonthly ?? Infinity) <= priceMax)
);
```

No refetch, no backend call, on any filter change — that's the whole design goal. Switching **category tabs** (Sale → Rent) does mean fetching the other file, but that's once per tab switch, not once per filter tweak.

## Refresh cadence

Same cron slot as the Projects feed — `homz export feed` runs once daily (10:00 IST, after enrichment), regenerating both the Projects files and these Properties files together in one run. There is no independent schedule to configure.
