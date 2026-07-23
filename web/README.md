# Homz Realtor — Frontend

Embeddable property search for **homzrealtor.com**. Zero dependencies, no build
step, one `<script type="module">` tag.

```
web/
├── homz-sdk.js      API client (also usable standalone)
├── homz-widget.js   <homz-search> web component
├── index.html       demo harness / dev page
└── README.md
```

---

## Quick start

The API serves this directory at `/web` when `HOMZ_API_SERVE_WEB=true`:

```bash
make api                       # or: docker compose up -d api
open http://localhost:8000/web/
```

Point it at a different backend with `?api=`:

```
http://localhost:8000/web/?api=https://api.homzrealtor.com
```

---

## Embedding on homzrealtor.com

```html
<script type="module" src="https://cdn.homzrealtor.com/homz-widget.js"></script>

<homz-search
  api="https://api.homzrealtor.com"
  city="gurgaon"
  listing-type="sale"
  page-size="24"
  sync-url>
</homz-search>
```

That's the whole integration. It works inside WordPress, React, Vue or plain
HTML — the widget renders in a **Shadow DOM**, so the host page's CSS can't
leak in and the widget's CSS can't leak out.

### Attributes

| Attribute | Default | Meaning |
|---|---|---|
| `api` | same origin | Base URL of the Homz API |
| `city` | — | Initial city filter (`gurgaon`, `noida`, …) |
| `listing-type` | `sale` | `sale` \| `rent` \| `new_launch` \| `resale` |
| `page-size` | `24` | Results per page |
| `sync-url` | off | Reflect filters into the address bar (shareable searches, working back button) |
| `compact` | off | Hide the filter rail — search box + results only |
| `theme` | `auto` | `light` \| `dark` \| `auto` (follows the OS) |

### Events

All bubble and cross the shadow boundary, so you can listen on `document`.

```js
const el = document.querySelector('homz-search');

// A card was clicked or activated by keyboard.
el.addEventListener('homz:select', (e) => {
  location.href = `/property/${e.detail.id}`;   // your routing
});

// Results landed — useful for analytics or a result count in your own chrome.
el.addEventListener('homz:results', (e) => {
  console.log(e.detail.total, e.detail.query);
});

el.addEventListener('homz:error', (e) => console.error(e.detail));
```

### Methods

```js
el.setFilter({ city: 'noida', bedrooms_min: 3 });  // merge filters, re-search
el.query      // current filter state
el.results    // current page of rows
```

---

## Theming

Override the CSS custom properties from the host page — they pierce the Shadow
DOM by design:

```css
homz-search {
  --homz-accent: #0f6b4f;
  --homz-radius: 6px;
  --homz-font: "Your Brand Font", system-ui, sans-serif;
  --homz-bg: #fff;
  --homz-surface: #f6f7f9;
  --homz-border: #e2e5ea;
  --homz-text: #14161a;
  --homz-muted: #6b7280;
}
```

Score badges use `--homz-good` / `--homz-mid` / `--homz-bad` and their
`-soft` background variants.

---

## Using the SDK on its own

If you're building your own UI and only want the API client:

```js
import { HomzClient, formatINR, priceLabel } from './homz-sdk.js';

const homz = new HomzClient({ baseUrl: 'https://api.homzrealtor.com' });

const page = await homz.properties({
  city: 'gurgaon',
  listing_type: 'sale',
  bedrooms_min: 3,
  price_max: 25000000,
  possession_status: ['ready_to_move'],
  max_risk_score: 40,
  sort: 'investment',
});

console.log(page.total, page.results.map(priceLabel));
console.log(formatINR(12500000));   // "₹1.25 Cr"
```

It also loads as a plain `<script>` (exposes `window.Homz`) or via CommonJS.

> **Running these files under Node?** The repo root `package.json` declares
> `"type": "commonjs"` for the legacy Puppeteer scrapers, so Node treats every
> `.js` file here as CommonJS and `import` fails. Browsers are unaffected —
> they follow `type="module"` on the script tag. For Node tooling, copy to
> `.mjs` or run from a directory with its own `"type": "module"` package.json.

**Available methods:** `properties`, `property`, `facets`, `autocomplete`,
`builders`, `builder`, `projects`, `reddit`, `redditComments`, `marketTrends`,
`rentalYield`, `supplyDemand`, `marketInsights`, `newLaunches`, `health`,
`stats`.

### Why the SDK isn't just `fetch`

Two things it handles that bite every hand-rolled search UI:

- **Request de-duplication.** A search box fires overlapping requests as the
  user types. Without cancellation, a slow response for `"gur"` can land *after*
  the fast one for `"gurgaon"` and overwrite correct results with stale ones.
  Each method passes a `key`, and a newer request aborts the older one.
- **Indian number formatting that matches the backend.** `formatINR` mirrors
  `homz.common.parsing.format_price_inr` exactly, so ₹1,25,00,000 never renders
  as "1.25 Cr" in one place and "₹12,500,000" in another. Digit grouping is
  Indian (`45,00,000`), not Western (`4,500,000`).

---

## CORS

The API allows the origins in `HOMZ_API_CORS_ORIGINS` plus any
`*.homzrealtor.com` subdomain. Add your staging domain there:

```bash
HOMZ_API_CORS_ORIGINS=https://www.homzrealtor.com,https://staging.homzrealtor.com
```

`allow_credentials` is off — every endpoint is public and read-only, so no
cookies are involved.

---

## Production notes

- **Serve from a CDN**, not from the API. Set `HOMZ_API_SERVE_WEB=false` and
  upload `homz-sdk.js` + `homz-widget.js` to your static host. `/web` exists
  for development convenience.
- **Cache the two JS files** aggressively and version the filename on deploy;
  they change far less often than the data.
- **Images are hot-linked** from portal CDNs. Those URLs can rotate or block
  hot-linking. If image reliability matters, proxy or re-host them — and check
  the licensing position first (see COMPLIANCE.md §4).
- **The widget is read-only.** It cannot trigger a scrape, so front-end traffic
  never becomes traffic against a source portal.
