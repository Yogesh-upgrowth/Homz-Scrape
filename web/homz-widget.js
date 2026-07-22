/**
 * <homz-search> — embeddable property search for homzrealtor.com
 *
 *   <script type="module" src="/web/homz-widget.js"></script>
 *   <homz-search api="https://api.homzrealtor.com" city="gurgaon" sync-url></homz-search>
 *
 * Attributes
 *   api            base URL of the Homz API (default: same origin)
 *   city           initial city filter
 *   listing-type   sale | rent | resale | new_launch
 *   page-size      results per page (default 24)
 *   sync-url       reflect filters into the address bar (shareable searches)
 *   compact        hide the filter rail — search + results only
 *   theme          light | dark | auto (default auto)
 *
 * Events (bubble, composed — listen on the element or on document)
 *   homz:select    { detail: property }   a card was activated
 *   homz:results   { detail: { total, query } }
 *   homz:error     { detail: HomzApiError }
 *
 * Why a web component with Shadow DOM: this has to drop into whatever
 * homzrealtor.com is built on — WordPress, React, plain HTML — without its CSS
 * colliding with the host page's, in either direction. Theming is exposed
 * through CSS custom properties rather than by leaking selectors.
 */

import {
  HomzClient,
  debounce,
  formatArea,
  formatPricePerSqft,
  locationLabel,
  possessionLabel,
  priceLabel,
  propertyTypeLabel,
  queryToSearchParams,
  relativeTime,
  scoreBand,
  searchParamsToQuery,
  titleCase,
} from './homz-sdk.js';

const CITIES = [
  ['', 'All NCR'],
  ['gurgaon', 'Gurgaon'],
  ['noida', 'Noida'],
  ['greater_noida', 'Greater Noida'],
  ['delhi', 'Delhi'],
  ['faridabad', 'Faridabad'],
  ['ghaziabad', 'Ghaziabad'],
  ['sohna', 'Sohna'],
];

const LISTING_TYPES = [
  ['sale', 'Buy'],
  ['rent', 'Rent'],
  ['new_launch', 'New Launch'],
  ['resale', 'Resale'],
];

const PROPERTY_TYPES = [
  'apartment', 'builder_floor', 'villa', 'independent_house', 'plot',
  'penthouse', 'studio', 'office', 'retail_shop',
];

const POSSESSION = ['ready_to_move', 'under_construction', 'new_launch', 'upcoming'];

const SORTS = [
  ['relevance', 'Best match'],
  ['newest', 'Newest first'],
  ['price_asc', 'Price: low to high'],
  ['price_desc', 'Price: high to low'],
  ['ppsf_asc', '₹/sq.ft: low to high'],
  ['area_desc', 'Largest first'],
  ['investment', 'Investment score'],
  ['lowest_risk', 'Lowest risk'],
];

// Budget steps in INR. Sale and rent need completely different scales — one
// shared control would be useless for both.
const BUDGET_SALE = [
  ['', 'Any'], [2500000, '25 L'], [5000000, '50 L'], [7500000, '75 L'],
  [10000000, '1 Cr'], [15000000, '1.5 Cr'], [20000000, '2 Cr'], [30000000, '3 Cr'],
  [50000000, '5 Cr'], [100000000, '10 Cr'], [250000000, '25 Cr'],
];

const BUDGET_RENT = [
  ['', 'Any'], [10000, '10 K'], [20000, '20 K'], [30000, '30 K'], [50000, '50 K'],
  [75000, '75 K'], [100000, '1 L'], [200000, '2 L'], [500000, '5 L'],
];

const STYLES = `
:host {
  --homz-font: system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
  --homz-radius: 12px;
  --homz-gap: 16px;
  --homz-bg: #ffffff;
  --homz-surface: #f7f8fa;
  --homz-border: #e3e6ec;
  --homz-text: #16181d;
  --homz-muted: #6b7280;
  --homz-accent: #b4531f;
  --homz-accent-soft: #fdf1e9;
  --homz-good: #17795e;
  --homz-good-soft: #e7f4ef;
  --homz-mid: #9a6b00;
  --homz-mid-soft: #fdf4e0;
  --homz-bad: #b3261e;
  --homz-bad-soft: #fdeceb;
  --homz-shadow: 0 1px 2px rgba(16,24,40,.06), 0 4px 12px rgba(16,24,40,.06);

  display: block;
  font-family: var(--homz-font);
  color: var(--homz-text);
  background: var(--homz-bg);
  container-type: inline-size;
}

:host([theme="dark"]), :host([data-theme="dark"]) {
  --homz-bg: #14161a;
  --homz-surface: #1c1f25;
  --homz-border: #2c313a;
  --homz-text: #e9ebef;
  --homz-muted: #9aa1ad;
  --homz-accent: #e8875a;
  --homz-accent-soft: #2a1d16;
  --homz-good-soft: #14261f;
  --homz-mid-soft: #2a2213;
  --homz-bad-soft: #2b1917;
  --homz-shadow: 0 1px 2px rgba(0,0,0,.4), 0 4px 14px rgba(0,0,0,.3);
}

*, *::before, *::after { box-sizing: border-box; }
button, input, select { font: inherit; color: inherit; }

.wrap { display: grid; gap: var(--homz-gap); }

/* ---- search bar ---- */
.searchbar { position: relative; display: flex; gap: 8px; flex-wrap: wrap; }
.field {
  flex: 1 1 260px; position: relative; display: flex; align-items: center;
  background: var(--homz-surface); border: 1px solid var(--homz-border);
  border-radius: var(--homz-radius); padding: 0 12px; min-height: 46px;
}
.field:focus-within { border-color: var(--homz-accent); box-shadow: 0 0 0 3px var(--homz-accent-soft); }
.field input { flex: 1; border: 0; background: transparent; outline: none; padding: 12px 0; min-width: 0; }
.field svg { width: 18px; height: 18px; flex: none; color: var(--homz-muted); }

.tabs { display: flex; gap: 4px; background: var(--homz-surface); padding: 4px;
        border-radius: var(--homz-radius); border: 1px solid var(--homz-border); }
.tab { border: 0; background: transparent; padding: 9px 14px; border-radius: 8px;
       cursor: pointer; color: var(--homz-muted); font-weight: 500; white-space: nowrap; }
.tab[aria-selected="true"] { background: var(--homz-bg); color: var(--homz-text);
                             box-shadow: var(--homz-shadow); }
.tab:focus-visible { outline: 2px solid var(--homz-accent); outline-offset: 1px; }

/* ---- autocomplete ---- */
.suggest {
  position: absolute; z-index: 30; top: calc(100% + 4px); left: 0; right: 0;
  background: var(--homz-bg); border: 1px solid var(--homz-border);
  border-radius: var(--homz-radius); box-shadow: var(--homz-shadow);
  max-height: 320px; overflow-y: auto; padding: 4px;
}
.suggest[hidden] { display: none; }
.suggest li { list-style: none; }
.suggest button {
  width: 100%; text-align: left; border: 0; background: transparent; cursor: pointer;
  padding: 10px 12px; border-radius: 8px; display: flex; justify-content: space-between;
  gap: 12px; align-items: center;
}
.suggest button:hover, .suggest button[aria-selected="true"] { background: var(--homz-surface); }
.suggest .kind { font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
                 color: var(--homz-muted); flex: none; }

/* ---- layout ---- */
.body { display: grid; gap: var(--homz-gap); grid-template-columns: 260px minmax(0,1fr); }
@container (max-width: 860px) { .body { grid-template-columns: minmax(0,1fr); } }
:host([compact]) .body { grid-template-columns: minmax(0,1fr); }
:host([compact]) .rail { display: none; }

.rail { display: grid; gap: 18px; align-content: start; }
.group { display: grid; gap: 8px; }
.group > h4 { margin: 0; font-size: 12px; letter-spacing: .05em; text-transform: uppercase;
              color: var(--homz-muted); font-weight: 600; }
select, .chip {
  border: 1px solid var(--homz-border); background: var(--homz-surface);
  border-radius: 9px; padding: 9px 11px; width: 100%; cursor: pointer;
}
select:focus-visible, .chip:focus-visible { outline: 2px solid var(--homz-accent); outline-offset: 1px; }
.range { display: flex; gap: 8px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { width: auto; padding: 6px 11px; border-radius: 999px; font-size: 13px; }
.chip[aria-pressed="true"] { background: var(--homz-accent-soft); border-color: var(--homz-accent);
                             color: var(--homz-accent); font-weight: 600; }
.checkline { display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: 14px; }

/* ---- results ---- */
.toolbar { display: flex; justify-content: space-between; align-items: center;
           gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
.count { color: var(--homz-muted); font-size: 14px; }
.count strong { color: var(--homz-text); }
.toolbar select { width: auto; }

.grid { display: grid; gap: var(--homz-gap);
        grid-template-columns: repeat(auto-fill, minmax(258px, 1fr)); }

.card {
  border: 1px solid var(--homz-border); border-radius: var(--homz-radius);
  background: var(--homz-bg); overflow: hidden; display: flex; flex-direction: column;
  text-align: left; padding: 0; cursor: pointer; transition: box-shadow .15s, transform .15s;
}
.card:hover { box-shadow: var(--homz-shadow); transform: translateY(-2px); }
.card:focus-visible { outline: 2px solid var(--homz-accent); outline-offset: 2px; }

.thumb { aspect-ratio: 16/10; background: var(--homz-surface); position: relative; overflow: hidden; }
.thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.thumb .ph { width: 100%; height: 100%; display: grid; place-items: center; color: var(--homz-muted); }
.ribbon { position: absolute; top: 10px; left: 10px; display: flex; gap: 6px; flex-wrap: wrap; }
.pill { font-size: 11px; font-weight: 600; padding: 4px 8px; border-radius: 999px;
        background: rgba(255,255,255,.94); color: #16181d; letter-spacing: .01em; }
.pill.rera { background: var(--homz-good); color: #fff; }

.cbody { padding: 13px 14px 15px; display: grid; gap: 7px; }
.price { font-size: 18px; font-weight: 700; letter-spacing: -.01em; }
.ppsf { font-size: 12px; color: var(--homz-muted); font-weight: 400; margin-left: 6px; }
.title { font-size: 14px; font-weight: 600; line-height: 1.35;
         display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.meta { font-size: 13px; color: var(--homz-muted); display: flex; flex-wrap: wrap; gap: 4px 8px; }
.meta span + span::before { content: '·'; margin-right: 8px; color: var(--homz-border); }
.builder { font-size: 12px; color: var(--homz-muted); }

.scores { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 2px; }
.score { font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 6px;
         display: inline-flex; gap: 5px; align-items: baseline; }
.score .n { font-variant-numeric: tabular-nums; }
.score.good { background: var(--homz-good-soft); color: var(--homz-good); }
.score.mid  { background: var(--homz-mid-soft);  color: var(--homz-mid); }
.score.bad  { background: var(--homz-bad-soft);  color: var(--homz-bad); }
.score.none { background: var(--homz-surface);   color: var(--homz-muted); }

/* ---- states ---- */
.state { padding: 56px 20px; text-align: center; color: var(--homz-muted);
         border: 1px dashed var(--homz-border); border-radius: var(--homz-radius); }
.state h3 { margin: 0 0 6px; color: var(--homz-text); font-size: 16px; }
.state p { margin: 0; font-size: 14px; }
.state button { margin-top: 14px; }

.skeleton { border: 1px solid var(--homz-border); border-radius: var(--homz-radius); overflow: hidden; }
.skeleton .thumb, .skeleton .line {
  background: linear-gradient(90deg, var(--homz-surface) 25%, var(--homz-border) 37%, var(--homz-surface) 63%);
  background-size: 400% 100%; animation: shimmer 1.4s ease infinite;
}
.skeleton .lines { padding: 14px; display: grid; gap: 8px; }
.skeleton .line { height: 12px; border-radius: 4px; }
.skeleton .line.w60 { width: 60%; } .skeleton .line.w40 { width: 40%; }
@keyframes shimmer { 0% { background-position: 100% 0; } 100% { background-position: 0 0; } }
@media (prefers-reduced-motion: reduce) {
  .skeleton .thumb, .skeleton .line { animation: none; }
  .card { transition: none; }
}

/* ---- pagination ---- */
.pager { display: flex; justify-content: center; align-items: center; gap: 8px;
         margin-top: 22px; flex-wrap: wrap; }
.pager button { border: 1px solid var(--homz-border); background: var(--homz-bg);
                border-radius: 9px; padding: 9px 14px; cursor: pointer; }
.pager button[disabled] { opacity: .45; cursor: default; }
.pager button:focus-visible { outline: 2px solid var(--homz-accent); outline-offset: 1px; }
.pager .at { color: var(--homz-muted); font-size: 14px; padding: 0 4px; }

.sr { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
      overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
`;

const ICON_SEARCH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>';
const ICON_HOME = '<svg viewBox="0 0 24 24" width="36" height="36" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/></svg>';

class HomzSearch extends HTMLElement {
  static observedAttributes = ['api', 'city', 'listing-type', 'page-size', 'theme'];

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._query = {};
    this._facets = null;
    this._state = 'idle';
    this._page = { total: 0, page: 1, pages: 0, results: [] };
    this._suggestions = [];
    this._suggestIndex = -1;
    this._reqId = 0;
  }

  // -- lifecycle ----------------------------------------------------------

  connectedCallback() {
    this.client = new HomzClient({
      baseUrl: this.getAttribute('api') || '',
      onError: (err) => this._emit('homz:error', err),
    });

    this._query = {
      listing_type: this.getAttribute('listing-type') || 'sale',
      city: this.getAttribute('city') || '',
      sort: 'relevance',
      page: 1,
      page_size: Number(this.getAttribute('page-size')) || 24,
    };

    if (this.hasAttribute('sync-url')) {
      Object.assign(this._query, searchParamsToQuery(new URLSearchParams(location.search)));
      this._onPop = () => {
        Object.assign(this._query, searchParamsToQuery(new URLSearchParams(location.search)));
        this._render();
        this._fetch({ pushUrl: false });
      };
      window.addEventListener('popstate', this._onPop);
    }

    this._applyTheme();
    this._debouncedSuggest = debounce((term) => this._suggest(term), 220);
    this._render();
    this._fetch({ pushUrl: false });
  }

  disconnectedCallback() {
    if (this._onPop) window.removeEventListener('popstate', this._onPop);
    this._debouncedSuggest?.cancel();
    this.client?.cancelAll();
    this._mq?.removeEventListener('change', this._onScheme);
  }

  attributeChangedCallback(name, oldValue, newValue) {
    if (oldValue === newValue || !this.client) return;
    if (name === 'api') this.client.options.baseUrl = String(newValue || '').replace(/\/+$/, '');
    if (name === 'theme') this._applyTheme();
    if (name === 'city') this.setFilter({ city: newValue || '' });
    if (name === 'listing-type') this.setFilter({ listing_type: newValue || 'sale' });
  }

  _applyTheme() {
    const theme = this.getAttribute('theme') || 'auto';
    if (theme !== 'auto') {
      this.dataset.theme = theme;
      return;
    }
    this._mq ||= window.matchMedia('(prefers-color-scheme: dark)');
    this._onScheme = (e) => { this.dataset.theme = e.matches ? 'dark' : 'light'; };
    this.dataset.theme = this._mq.matches ? 'dark' : 'light';
    this._mq.addEventListener('change', this._onScheme);
  }

  // -- public API ---------------------------------------------------------

  /** Merge filters and re-search. Resets to page 1 unless `page` is given. */
  setFilter(patch = {}) {
    const resetsPage = !('page' in patch);
    Object.assign(this._query, patch);
    if (resetsPage) this._query.page = 1;
    this._render();
    this._fetch();
  }

  get query() { return { ...this._query }; }
  get results() { return this._page.results; }

  // -- data ---------------------------------------------------------------

  async _fetch({ pushUrl = true } = {}) {
    const id = ++this._reqId;
    this._state = 'loading';
    this._renderResults();

    if (pushUrl && this.hasAttribute('sync-url')) {
      const params = queryToSearchParams(this._query);
      history.pushState(null, '', `${location.pathname}?${params}`);
    }

    try {
      const [page, facets] = await Promise.all([
        this.client.properties(this._query),
        this.client.facets({
          q: this._query.q,
          city: this._query.city,
          listing_type: this._query.listing_type,
        }).catch(() => null),
      ]);
      if (id !== this._reqId) return; // a newer search already landed

      this._page = page;
      if (facets) this._facets = facets;
      this._state = page.results.length ? 'ready' : 'empty';
      this._emit('homz:results', { total: page.total, query: this.query });
    } catch (err) {
      if (err.name === 'AbortError' || id !== this._reqId) return;
      this._state = 'error';
      this._error = err;
    }
    this._render();
  }

  async _suggest(term) {
    if (!term || term.length < 2) {
      this._suggestions = [];
      this._renderSuggest();
      return;
    }
    try {
      this._suggestions = await this.client.autocomplete(term, 8);
      this._suggestIndex = -1;
      this._renderSuggest();
    } catch {
      this._suggestions = [];
      this._renderSuggest();
    }
  }

  _emit(name, detail) {
    this.dispatchEvent(new CustomEvent(name, { detail, bubbles: true, composed: true }));
  }

  // -- render -------------------------------------------------------------

  _render() {
    if (!this._built) {
      const style = document.createElement('style');
      style.textContent = STYLES;
      this.shadowRoot.append(style);
      this._root = document.createElement('div');
      this._root.className = 'wrap';
      this.shadowRoot.append(this._root);
      this._built = true;
    }
    this._root.innerHTML = `
      ${this._searchbarHtml()}
      <div class="body">
        <aside class="rail" aria-label="Filters">${this._railHtml()}</aside>
        <section class="results" aria-live="polite" aria-busy="${this._state === 'loading'}">
          ${this._resultsHtml()}
        </section>
      </div>`;
    this._bind();
  }

  _renderResults() {
    const section = this._root?.querySelector('.results');
    if (!section) return;
    section.setAttribute('aria-busy', String(this._state === 'loading'));
    section.innerHTML = this._resultsHtml();
    this._bindResults();
  }

  _searchbarHtml() {
    const q = escapeAttr(this._query.q || '');
    return `
      <div class="searchbar">
        <div class="tabs" role="tablist" aria-label="Listing type">
          ${LISTING_TYPES.map(([value, label]) => `
            <button class="tab" role="tab" data-lt="${value}"
                    aria-selected="${this._query.listing_type === value}">${label}</button>`).join('')}
        </div>
        <div class="field">
          ${ICON_SEARCH}
          <input type="search" id="q" value="${q}" autocomplete="off"
                 role="combobox" aria-expanded="false" aria-controls="suggest"
                 aria-label="Search by project, builder, sector or keyword"
                 placeholder="Try &quot;Godrej Sector 49&quot; or &quot;Dwarka Expressway&quot;">
          <ul class="suggest" id="suggest" role="listbox" hidden></ul>
        </div>
      </div>`;
  }

  _railHtml() {
    const q = this._query;
    const budget = q.listing_type === 'rent' ? BUDGET_RENT : BUDGET_SALE;
    const facetSectors = this._facets?.sector?.slice(0, 14) || [];

    return `
      <div class="group">
        <h4>City</h4>
        <select id="city" aria-label="City">
          ${CITIES.map(([v, l]) =>
            `<option value="${v}" ${q.city === v ? 'selected' : ''}>${l}</option>`).join('')}
        </select>
      </div>

      <div class="group">
        <h4>Budget</h4>
        <div class="range">
          <select id="price_min" aria-label="Minimum budget">
            ${budget.map(([v, l]) =>
              `<option value="${v}" ${String(q.price_min ?? '') === String(v) ? 'selected' : ''}>${v === '' ? 'Min' : l}</option>`).join('')}
          </select>
          <select id="price_max" aria-label="Maximum budget">
            ${budget.map(([v, l]) =>
              `<option value="${v}" ${String(q.price_max ?? '') === String(v) ? 'selected' : ''}>${v === '' ? 'Max' : l}</option>`).join('')}
          </select>
        </div>
      </div>

      <div class="group">
        <h4>Bedrooms</h4>
        <div class="chips" role="group" aria-label="Bedrooms">
          ${[1, 2, 3, 4, 5].map((n) => `
            <button class="chip" data-bhk="${n}"
                    aria-pressed="${q.bedrooms_min === n}">${n}${n === 5 ? '+' : ''} BHK</button>`).join('')}
        </div>
      </div>

      <div class="group">
        <h4>Property type</h4>
        <div class="chips" role="group" aria-label="Property type">
          ${PROPERTY_TYPES.map((t) => `
            <button class="chip" data-ptype="${t}"
                    aria-pressed="${(q.property_type || []).includes(t)}">${propertyTypeLabel(t)}</button>`).join('')}
        </div>
      </div>

      <div class="group">
        <h4>Possession</h4>
        <div class="chips" role="group" aria-label="Possession status">
          ${POSSESSION.map((p) => `
            <button class="chip" data-poss="${p}"
                    aria-pressed="${(q.possession_status || []).includes(p)}">${possessionLabel(p)}</button>`).join('')}
        </div>
      </div>

      ${facetSectors.length ? `
      <div class="group">
        <h4>Sector</h4>
        <div class="chips" role="group" aria-label="Sector">
          ${facetSectors.map((f) => `
            <button class="chip" data-sector="${escapeAttr(f.value)}"
                    aria-pressed="${q.sector === f.value}">${escapeHtml(f.value)}
              <span class="sr">, ${f.count} listings</span></button>`).join('')}
        </div>
      </div>` : ''}

      <div class="group">
        <h4>Quality</h4>
        <label class="checkline">
          <input type="checkbox" id="has_rera" ${q.has_rera ? 'checked' : ''}>
          RERA registered only
        </label>
        <label class="checkline">
          <input type="checkbox" id="low_risk" ${q.max_risk_score ? 'checked' : ''}>
          Lower risk only
        </label>
      </div>

      <button class="chip" id="reset" style="width:100%;border-radius:9px">Clear all filters</button>`;
  }

  _resultsHtml() {
    if (this._state === 'loading') {
      return `<div class="toolbar"><span class="count">Searching…</span></div>
              <div class="grid">${Array.from({ length: 6 }, () => `
                <div class="skeleton"><div class="thumb"></div>
                  <div class="lines"><div class="line w40"></div><div class="line"></div>
                  <div class="line w60"></div></div></div>`).join('')}</div>`;
    }

    if (this._state === 'error') {
      const offline = this._error?.status === 0;
      return `<div class="state" role="alert">
          <h3>${offline ? 'Can’t reach the server' : 'Something went wrong'}</h3>
          <p>${escapeHtml(this._error?.message || 'Unknown error')}</p>
          <button class="chip" id="retry">Try again</button>
        </div>`;
    }

    if (this._state === 'empty') {
      return `<div class="state">
          <h3>No properties match those filters</h3>
          <p>Try widening the budget, or clearing a filter or two.</p>
          <button class="chip" id="reset2">Clear all filters</button>
        </div>`;
    }

    const { total, page, pages, results } = this._page;
    return `
      <div class="toolbar">
        <span class="count"><strong>${total.toLocaleString('en-IN')}</strong>
          ${total === 1 ? 'property' : 'properties'}${this._query.city ? ` in ${titleCase(this._query.city)}` : ''}</span>
        <label class="sr" for="sort">Sort by</label>
        <select id="sort">
          ${SORTS.map(([v, l]) =>
            `<option value="${v}" ${this._query.sort === v ? 'selected' : ''}>${l}</option>`).join('')}
        </select>
      </div>
      <div class="grid">${results.map((r) => this._cardHtml(r)).join('')}</div>
      ${pages > 1 ? `
        <nav class="pager" aria-label="Pagination">
          <button data-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>← Previous</button>
          <span class="at">Page ${page} of ${pages}</span>
          <button data-page="${page + 1}" ${page >= pages ? 'disabled' : ''}>Next →</button>
        </nav>` : ''}`;
  }

  _cardHtml(row) {
    const price = priceLabel(row);
    const ppsf = formatPricePerSqft(row.price_per_sqft);
    const meta = [row.configuration, formatArea(row.area_sqft), propertyTypeLabel(row.property_type)]
      .filter(Boolean);
    const listed = relativeTime(row.listed_at || row.first_seen_at);

    return `
      <article class="card" tabindex="0" role="link" data-id="${row.id}"
               aria-label="${escapeAttr(`${price}, ${row.configuration || ''} in ${locationLabel(row)}`)}">
        <div class="thumb">
          ${row.primary_image
            ? `<img src="${escapeAttr(row.primary_image)}" alt="" loading="lazy" decoding="async">`
            : `<div class="ph">${ICON_HOME}</div>`}
          <div class="ribbon">
            <span class="pill">${possessionLabel(row.possession_status)}</span>
            ${row.rera_number ? '<span class="pill rera">RERA</span>' : ''}
          </div>
        </div>
        <div class="cbody">
          <div class="price">${escapeHtml(price)}${ppsf ? `<span class="ppsf">${ppsf}</span>` : ''}</div>
          <div class="title">${escapeHtml(row.project_name || row.title || 'Property')}</div>
          <div class="meta">${meta.map((m) => `<span>${escapeHtml(m)}</span>`).join('')}</div>
          <div class="meta"><span>${escapeHtml(locationLabel(row))}</span>${
            row.micro_market ? `<span>${escapeHtml(row.micro_market)}</span>` : ''}</div>
          ${row.builder_name ? `<div class="builder">by ${escapeHtml(row.builder_name)}</div>` : ''}
          <div class="scores">
            ${scoreChip('Investment', row.investment_score)}
            ${scoreChip('Risk', row.risk_score, { inverted: true })}
            ${scoreChip('Location', row.location_score)}
          </div>
          ${listed ? `<div class="builder">Listed ${listed}</div>` : ''}
        </div>
      </article>`;
  }

  _renderSuggest() {
    const box = this._root?.querySelector('#suggest');
    const input = this._root?.querySelector('#q');
    if (!box || !input) return;

    if (!this._suggestions.length) {
      box.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      return;
    }
    box.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    box.innerHTML = this._suggestions.map((s, i) => `
      <li role="option" aria-selected="${i === this._suggestIndex}">
        <button type="button" data-value="${escapeAttr(s.value)}" data-kind="${s.kind}"
                aria-selected="${i === this._suggestIndex}">
          <span>${escapeHtml(s.value)}</span><span class="kind">${s.kind}</span>
        </button>
      </li>`).join('');

    box.querySelectorAll('button').forEach((btn) => {
      btn.addEventListener('mousedown', (e) => {
        e.preventDefault(); // keep focus so blur doesn't close before click lands
        this._applySuggestion(btn.dataset.value, btn.dataset.kind);
      });
    });
  }

  _applySuggestion(value, kind) {
    this._suggestions = [];
    this._renderSuggest();
    const patch = { q: '' };
    if (kind === 'builder') patch.builder = value;
    else if (kind === 'project') patch.project = value;
    else if (kind === 'locality') patch.sector = value;
    else patch.q = value;
    this.setFilter(patch);
  }

  // -- events -------------------------------------------------------------

  _bind() {
    const root = this._root;
    const q = root.querySelector('#q');

    q.addEventListener('input', (e) => {
      this._query.q = e.target.value;
      this._debouncedSuggest(e.target.value);
    });
    q.addEventListener('keydown', (e) => this._onSearchKey(e));
    q.addEventListener('blur', () => setTimeout(() => {
      this._suggestions = []; this._renderSuggest();
    }, 120));

    root.querySelectorAll('.tab').forEach((tab) => {
      tab.addEventListener('click', () => {
        // Budget scales differ between buy and rent; carrying a sale budget
        // into rent would filter everything out.
        this.setFilter({ listing_type: tab.dataset.lt, price_min: '', price_max: '' });
      });
    });

    root.querySelector('#city')?.addEventListener('change', (e) =>
      this.setFilter({ city: e.target.value, sector: '' }));
    root.querySelector('#price_min')?.addEventListener('change', (e) =>
      this.setFilter({ price_min: e.target.value }));
    root.querySelector('#price_max')?.addEventListener('change', (e) =>
      this.setFilter({ price_max: e.target.value }));

    root.querySelectorAll('[data-bhk]').forEach((chip) => {
      chip.addEventListener('click', () => {
        const n = Number(chip.dataset.bhk);
        this.setFilter({ bedrooms_min: this._query.bedrooms_min === n ? undefined : n });
      });
    });

    root.querySelectorAll('[data-ptype]').forEach((chip) => {
      chip.addEventListener('click', () =>
        this.setFilter({ property_type: toggle(this._query.property_type, chip.dataset.ptype) }));
    });
    root.querySelectorAll('[data-poss]').forEach((chip) => {
      chip.addEventListener('click', () =>
        this.setFilter({ possession_status: toggle(this._query.possession_status, chip.dataset.poss) }));
    });
    root.querySelectorAll('[data-sector]').forEach((chip) => {
      chip.addEventListener('click', () => {
        const v = chip.dataset.sector;
        this.setFilter({ sector: this._query.sector === v ? '' : v });
      });
    });

    root.querySelector('#has_rera')?.addEventListener('change', (e) =>
      this.setFilter({ has_rera: e.target.checked ? true : undefined }));
    root.querySelector('#low_risk')?.addEventListener('change', (e) =>
      this.setFilter({ max_risk_score: e.target.checked ? 40 : undefined }));

    root.querySelector('#reset')?.addEventListener('click', () => this._reset());

    this._bindResults();
  }

  _bindResults() {
    const root = this._root;
    root.querySelector('#sort')?.addEventListener('change', (e) =>
      this.setFilter({ sort: e.target.value }));
    root.querySelector('#retry')?.addEventListener('click', () => this._fetch());
    root.querySelector('#reset2')?.addEventListener('click', () => this._reset());

    root.querySelectorAll('.pager button[data-page]').forEach((btn) => {
      btn.addEventListener('click', () => {
        this.setFilter({ page: Number(btn.dataset.page) });
        this.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    });

    root.querySelectorAll('.card').forEach((card) => {
      const open = () => {
        const row = this._page.results.find((r) => String(r.id) === card.dataset.id);
        if (row) this._emit('homz:select', row);
      };
      card.addEventListener('click', open);
      card.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
      });
    });
  }

  _onSearchKey(e) {
    const max = this._suggestions.length - 1;
    if (e.key === 'ArrowDown' && max >= 0) {
      e.preventDefault();
      this._suggestIndex = Math.min(this._suggestIndex + 1, max);
      this._renderSuggest();
    } else if (e.key === 'ArrowUp' && max >= 0) {
      e.preventDefault();
      this._suggestIndex = Math.max(this._suggestIndex - 1, -1);
      this._renderSuggest();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const picked = this._suggestions[this._suggestIndex];
      if (picked) this._applySuggestion(picked.value, picked.kind);
      else { this._debouncedSuggest.cancel(); this.setFilter({ q: e.target.value }); }
    } else if (e.key === 'Escape') {
      this._suggestions = [];
      this._renderSuggest();
    }
  }

  _reset() {
    this._query = {
      listing_type: this._query.listing_type,
      sort: 'relevance',
      page: 1,
      page_size: this._query.page_size,
      city: this.getAttribute('city') || '',
    };
    this._render();
    this._fetch();
  }
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function scoreChip(label, value, opts = {}) {
  const band = scoreBand(value, opts);
  const shown = value === null || value === undefined ? '—' : Math.round(Number(value));
  return `<span class="score ${band}" title="${label} score: ${shown} out of 100">
    ${label}<span class="n">${shown}</span></span>`;
}

function toggle(list, value) {
  const set = new Set(list || []);
  if (set.has(value)) set.delete(value); else set.add(value);
  return [...set];
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function escapeAttr(value) {
  return escapeHtml(value);
}

if (!customElements.get('homz-search')) {
  customElements.define('homz-search', HomzSearch);
}

export { HomzSearch };
export default HomzSearch;
