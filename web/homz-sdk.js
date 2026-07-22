/**
 * Homz Realtor — JavaScript SDK
 *
 * Zero dependencies, no build step. Works as an ES module, a <script> tag
 * (exposes `window.Homz`), or via CommonJS.
 *
 *   import { HomzClient, formatINR } from './homz-sdk.js';
 *   const homz = new HomzClient({ baseUrl: 'https://api.homzrealtor.com' });
 *   const page = await homz.properties({ city: 'gurgaon', bedrooms_min: 3 });
 *
 * Design notes:
 *   - Every request is abortable and de-duplicated by key, because a search UI
 *     fires overlapping requests as the user types; without this you get
 *     out-of-order responses overwriting newer results.
 *   - Money helpers mirror the backend's `format_price_inr` exactly, so the
 *     same number never renders two ways across the stack.
 */

const DEFAULTS = {
  baseUrl: '',
  timeout: 20000,
  retries: 2,
  token: null,
  onError: null,
};

/** Thrown for any non-2xx response. Carries status so callers can branch. */
export class HomzApiError extends Error {
  constructor(message, { status = 0, url = '', body = null } = {}) {
    super(message);
    this.name = 'HomzApiError';
    this.status = status;
    this.url = url;
    this.body = body;
  }

  /** Worth retrying: transient network/server conditions only. */
  get isRetryable() {
    return this.status === 0 || this.status === 429 || this.status >= 500;
  }
}

export class HomzClient {
  constructor(options = {}) {
    this.options = { ...DEFAULTS, ...options };
    this.options.baseUrl = String(this.options.baseUrl || '').replace(/\/+$/, '');
    /** @type {Map<string, AbortController>} */
    this._inflight = new Map();
  }

  // -- core ---------------------------------------------------------------

  /**
   * @param {string} path
   * @param {object} [params]
   * @param {{ key?: string, signal?: AbortSignal }} [opts]
   *   `key` cancels any earlier in-flight request with the same key — this is
   *   what stops a slow "gur" response from landing after a fast "gurgaon" one.
   */
  async get(path, params = {}, opts = {}) {
    const url = this._buildUrl(path, params);
    const key = opts.key;

    if (key && this._inflight.has(key)) {
      this._inflight.get(key).abort();
    }

    const controller = new AbortController();
    if (key) this._inflight.set(key, controller);
    if (opts.signal) {
      opts.signal.addEventListener('abort', () => controller.abort(), { once: true });
    }

    const timer = setTimeout(() => controller.abort(), this.options.timeout);
    let lastError = null;

    try {
      for (let attempt = 0; attempt <= this.options.retries; attempt += 1) {
        try {
          const response = await fetch(url, {
            method: 'GET',
            signal: controller.signal,
            headers: this._headers(),
            credentials: 'omit',
          });

          if (!response.ok) {
            const body = await safeJson(response);
            const error = new HomzApiError(
              body?.detail || `HTTP ${response.status} from ${path}`,
              { status: response.status, url, body },
            );
            // 4xx will never succeed on retry — fail fast.
            if (!error.isRetryable || attempt === this.options.retries) throw error;
            lastError = error;
            await sleep(backoffMs(attempt));
            continue;
          }

          return await response.json();
        } catch (err) {
          if (err.name === 'AbortError') throw err;
          if (err instanceof HomzApiError) {
            if (!err.isRetryable || attempt === this.options.retries) throw err;
            lastError = err;
          } else {
            // Network-level failure (offline, DNS, CORS).
            lastError = new HomzApiError(err.message || 'network error', { url });
            if (attempt === this.options.retries) throw lastError;
          }
          await sleep(backoffMs(attempt));
        }
      }
      throw lastError || new HomzApiError('request failed', { url });
    } catch (err) {
      if (err.name !== 'AbortError' && typeof this.options.onError === 'function') {
        this.options.onError(err);
      }
      throw err;
    } finally {
      clearTimeout(timer);
      if (key && this._inflight.get(key) === controller) this._inflight.delete(key);
    }
  }

  _headers() {
    const headers = { Accept: 'application/json' };
    if (this.options.token) headers.Authorization = `Bearer ${this.options.token}`;
    return headers;
  }

  _buildUrl(path, params) {
    const base = this.options.baseUrl || '';
    const url = new URL(`${base}${path}`, base || window.location.origin);
    for (const [key, value] of Object.entries(params || {})) {
      if (value === null || value === undefined || value === '') continue;
      // FastAPI reads repeated keys as a list — never comma-join.
      if (Array.isArray(value)) {
        value.filter((v) => v !== null && v !== undefined && v !== '')
          .forEach((v) => url.searchParams.append(key, String(v)));
      } else if (typeof value === 'boolean') {
        url.searchParams.set(key, value ? 'true' : 'false');
      } else {
        url.searchParams.set(key, String(value));
      }
    }
    return url.toString();
  }

  cancelAll() {
    this._inflight.forEach((c) => c.abort());
    this._inflight.clear();
  }

  // -- properties ---------------------------------------------------------

  /** @returns {Promise<{total:number,page:number,page_size:number,pages:number,results:object[]}>} */
  properties(query = {}, opts = {}) {
    return this.get('/properties', query, { key: 'properties', ...opts });
  }

  property(id, opts = {}) {
    return this.get(`/properties/${encodeURIComponent(id)}`, {}, opts);
  }

  facets(query = {}, opts = {}) {
    return this.get('/properties/facets', query, { key: 'facets', ...opts });
  }

  autocomplete(term, limit = 10, opts = {}) {
    return this.get('/autocomplete', { term, limit }, { key: 'autocomplete', ...opts });
  }

  // -- builders & projects ------------------------------------------------

  builders(query = {}, opts = {}) {
    return this.get('/builders', query, { key: 'builders', ...opts });
  }

  builder(id, opts = {}) {
    return this.get(`/builders/${encodeURIComponent(id)}`, {}, opts);
  }

  projects(query = {}, opts = {}) {
    return this.get('/projects', query, { key: 'projects', ...opts });
  }

  // -- discussion ---------------------------------------------------------

  reddit(query = {}, opts = {}) {
    return this.get('/reddit', query, { key: 'reddit', ...opts });
  }

  redditComments(sourceId, opts = {}) {
    return this.get(`/reddit/${encodeURIComponent(sourceId)}/comments`, {}, opts);
  }

  // -- market -------------------------------------------------------------

  marketTrends(query = {}, opts = {}) {
    return this.get('/market/trends', query, { key: 'trends', ...opts });
  }

  rentalYield(query = {}, opts = {}) {
    return this.get('/market/yield', query, opts);
  }

  supplyDemand(query = {}, opts = {}) {
    return this.get('/market/supply-demand', query, opts);
  }

  marketInsights(query = {}, opts = {}) {
    return this.get('/market/insights', query, opts);
  }

  newLaunches(days = 90, opts = {}) {
    return this.get('/market/new-launches', { days }, opts);
  }

  // -- ops ----------------------------------------------------------------

  health(opts = {}) {
    return this.get('/health', {}, opts);
  }

  stats(opts = {}) {
    return this.get('/stats', {}, opts);
  }
}

// ---------------------------------------------------------------------------
// formatting — mirrors homz.common.parsing.format_price_inr
// ---------------------------------------------------------------------------

/**
 * Render INR the way Indian buyers read it.
 *   12500000 → "₹1.25 Cr"   8500000 → "₹85 L"   45000 → "₹45,000"
 */
export function formatINR(value, { symbol = true } = {}) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return null;

  const prefix = symbol ? '₹' : '';
  if (n >= 1e7) return `${prefix}${trimZeros(n / 1e7)} Cr`;
  if (n >= 1e5) return `${prefix}${trimZeros(n / 1e5)} L`;
  return `${prefix}${formatIndianDigits(Math.round(n))}`;
}

/** Indian digit grouping: 4500000 → "45,00,000" (not "4,500,000"). */
export function formatIndianDigits(value) {
  const s = String(Math.round(Number(value)));
  if (s.length <= 3) return s;
  const last3 = s.slice(-3);
  const rest = s.slice(0, -3);
  return `${rest.replace(/\B(?=(\d{2})+(?!\d))/g, ',')},${last3}`;
}

function trimZeros(n) {
  return String(Number(n.toFixed(2)));
}

export function formatArea(sqft) {
  if (!sqft) return null;
  return `${formatIndianDigits(sqft)} sq.ft.`;
}

export function formatPricePerSqft(value) {
  if (!value) return null;
  return `₹${formatIndianDigits(value)}/sq.ft.`;
}

/** "sector 82" / "SECTOR 82" → "Sector 82"; enum values → readable labels. */
export function titleCase(value) {
  if (!value) return '';
  return String(value)
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

const POSSESSION_LABELS = {
  ready_to_move: 'Ready to Move',
  under_construction: 'Under Construction',
  new_launch: 'New Launch',
  upcoming: 'Upcoming',
  completed: 'Completed',
  unknown: 'Status Unknown',
};

const PROPERTY_TYPE_LABELS = {
  builder_floor: 'Builder Floor',
  independent_house: 'Independent House',
  serviced_apartment: 'Serviced Apartment',
  retail_shop: 'Retail Shop',
  co_working: 'Co-working',
};

export function possessionLabel(value) {
  return POSSESSION_LABELS[value] || titleCase(value);
}

export function propertyTypeLabel(value) {
  return PROPERTY_TYPE_LABELS[value] || titleCase(value);
}

export function locationLabel(row) {
  const parts = [row.sector || row.locality, titleCase(row.city)].filter(Boolean);
  return parts.join(', ');
}

/** Headline price: rent listings show a monthly figure, sales an absolute one. */
export function priceLabel(row) {
  if (row.listing_type === 'rent' || (row.rent_monthly && !row.price)) {
    const rent = formatINR(row.rent_monthly);
    return rent ? `${rent}/mo` : 'Rent on request';
  }
  if (row.is_price_on_request || (!row.price && !row.price_max)) return 'Price on request';
  const low = formatINR(row.price);
  if (row.price_max && Number(row.price_max) !== Number(row.price)) {
    return `${low} – ${formatINR(row.price_max)}`;
  }
  return low;
}

export function relativeTime(iso) {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  const days = Math.floor((Date.now() - then) / 86400000);
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 30) return `${days} days ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months} month${months > 1 ? 's' : ''} ago`;
  return `${Math.floor(months / 12)}y ago`;
}

/**
 * Score → band. The API returns 0-100; these thresholds are what the UI colours
 * on, kept here so the widget and any custom integration agree.
 */
export function scoreBand(score, { inverted = false } = {}) {
  if (score === null || score === undefined) return 'none';
  const n = Number(score);
  const good = inverted ? n <= 30 : n >= 70;
  const bad = inverted ? n >= 60 : n <= 35;
  if (good) return 'good';
  if (bad) return 'bad';
  return 'mid';
}

// ---------------------------------------------------------------------------
// query-state helpers — shareable, back-button-friendly searches
// ---------------------------------------------------------------------------

/** Filters that may legitimately repeat in a URL. */
export const ARRAY_PARAMS = new Set([
  'property_type', 'possession_status', 'segment', 'amenities', 'keywords', 'topics',
]);

const NUMERIC_PARAMS = new Set([
  'bedrooms_min', 'bedrooms_max', 'price_min', 'price_max', 'area_min', 'area_max',
  'min_investment_score', 'max_risk_score', 'page', 'page_size',
]);

const BOOLEAN_PARAMS = new Set(['is_commercial', 'has_rera']);

export function queryToSearchParams(query) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query || {})) {
    if (value === null || value === undefined || value === '') continue;
    if (Array.isArray(value)) {
      if (!value.length) continue;
      value.forEach((v) => params.append(key, String(v)));
    } else {
      params.set(key, String(value));
    }
  }
  return params;
}

export function searchParamsToQuery(params) {
  const query = {};
  for (const key of new Set(params.keys())) {
    if (ARRAY_PARAMS.has(key)) {
      query[key] = params.getAll(key);
    } else {
      const raw = params.get(key);
      if (NUMERIC_PARAMS.has(key)) {
        const n = Number(raw);
        query[key] = Number.isFinite(n) ? n : undefined;
      } else if (BOOLEAN_PARAMS.has(key)) {
        query[key] = raw === 'true';
      } else {
        query[key] = raw;
      }
    }
  }
  return query;
}

// ---------------------------------------------------------------------------
// misc
// ---------------------------------------------------------------------------

export function debounce(fn, wait = 250) {
  let timer = null;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
  wrapped.cancel = () => clearTimeout(timer);
  return wrapped;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function backoffMs(attempt) {
  // Full jitter, same shape as the Python retry middleware.
  return Math.random() * Math.min(4000, 400 * 2 ** attempt);
}

async function safeJson(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// exports for non-module consumers
// ---------------------------------------------------------------------------

const Homz = {
  HomzClient,
  HomzApiError,
  formatINR,
  formatIndianDigits,
  formatArea,
  formatPricePerSqft,
  titleCase,
  possessionLabel,
  propertyTypeLabel,
  locationLabel,
  priceLabel,
  relativeTime,
  scoreBand,
  queryToSearchParams,
  searchParamsToQuery,
  debounce,
  ARRAY_PARAMS,
};

if (typeof window !== 'undefined') window.Homz = Homz;
if (typeof module !== 'undefined' && module.exports) module.exports = Homz;

export default Homz;
