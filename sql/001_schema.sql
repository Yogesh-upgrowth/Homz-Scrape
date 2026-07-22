-- ===========================================================================
-- Homz Realtor — Real Estate Intelligence Platform
-- PostgreSQL 15+ schema.  Idempotent: safe to re-run.
--
-- Design notes
--   * `locations` is a dimension table; properties/projects reference it so
--     "all listings on Dwarka Expressway" is an index scan, not a LIKE scan.
--   * Natural keys are (source, source_id). Every ingest is an UPSERT on that
--     pair, which is what makes re-scraping idempotent.
--   * Money is NUMERIC(16,2) — never float. Areas are DOUBLE PRECISION sqft.
--   * Large/optional sub-documents (specs, raw payloads) live in JSONB.
--   * price_history is append-only and written by a trigger, so a price change
--     is captured even if application code forgets to.
-- ===========================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- fuzzy name search
CREATE EXTENSION IF NOT EXISTS btree_gin;    -- composite GIN indexes
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---------------------------------------------------------------------------
-- enums
-- ---------------------------------------------------------------------------

DO $$ BEGIN
    CREATE TYPE source_enum AS ENUM ('magicbricks','housing','squareyards','reddit');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE listing_type_enum AS ENUM
        ('sale','rent','resale','new_launch','project','commercial','pg','unknown');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE property_type_enum AS ENUM
        ('apartment','builder_floor','independent_house','villa','plot','penthouse',
         'studio','office','retail_shop','showroom','warehouse','co_working',
         'farmhouse','serviced_apartment','other');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE possession_status_enum AS ENUM
        ('ready_to_move','under_construction','new_launch','upcoming','completed','unknown');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE segment_enum AS ENUM
        ('affordable','mid','premium','luxury','ultra_luxury','unknown');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE seller_type_enum AS ENUM ('owner','agent','builder','unknown');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE sentiment_enum AS ENUM ('positive','negative','neutral','mixed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE city_enum AS ENUM
        ('gurgaon','noida','greater_noida','delhi','faridabad','ghaziabad',
         'sohna','other_ncr','unknown');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE job_status_enum AS ENUM ('running','success','partial','failed','blocked');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------------
-- shared trigger: updated_at
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ===========================================================================
-- locations (dimension)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS locations (
    id              BIGSERIAL PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    city            city_enum NOT NULL DEFAULT 'unknown',
    state           TEXT,
    locality        TEXT,
    sector          TEXT,
    sub_locality    TEXT,
    micro_market    TEXT,
    pincode         TEXT,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    -- rolling aggregates refreshed by the ETL job
    avg_price_per_sqft   NUMERIC(14,2),
    avg_rent_per_month   NUMERIC(14,2),
    listing_count        INTEGER NOT NULL DEFAULT 0,
    rental_yield_pct     NUMERIC(6,3),
    location_score       NUMERIC(5,2),
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT locations_lat_chk CHECK (latitude  IS NULL OR latitude  BETWEEN -90  AND 90),
    CONSTRAINT locations_lon_chk CHECK (longitude IS NULL OR longitude BETWEEN -180 AND 180)
);

CREATE INDEX IF NOT EXISTS idx_locations_city          ON locations (city);
CREATE INDEX IF NOT EXISTS idx_locations_sector        ON locations (city, sector);
CREATE INDEX IF NOT EXISTS idx_locations_micro_market  ON locations (micro_market);
CREATE INDEX IF NOT EXISTS idx_locations_locality_trgm ON locations USING gin (locality gin_trgm_ops);

DROP TRIGGER IF EXISTS trg_locations_updated ON locations;
CREATE TRIGGER trg_locations_updated BEFORE UPDATE ON locations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ===========================================================================
-- builders
-- ===========================================================================

CREATE TABLE IF NOT EXISTS builders (
    id                  BIGSERIAL PRIMARY KEY,
    source              source_enum NOT NULL,
    source_id           TEXT NOT NULL,
    profile_url         TEXT,

    name                TEXT NOT NULL,
    normalized_name     TEXT NOT NULL,
    description         TEXT,
    established_year    SMALLINT,
    headquarters        TEXT,
    website             TEXT,

    total_projects      INTEGER,
    ongoing_projects    INTEGER,
    completed_projects  INTEGER,
    upcoming_projects   INTEGER,

    rating              NUMERIC(3,2),
    rating_count        INTEGER,
    review_count        INTEGER,
    reviews             JSONB NOT NULL DEFAULT '[]'::jsonb,
    cities              TEXT[] NOT NULL DEFAULT '{}',

    contact_name        TEXT,
    contact_phone       TEXT,
    contact_email       TEXT,

    -- AI enrichment
    trust_score         NUMERIC(5,2),
    risk_score          NUMERIC(5,2),
    sentiment           sentiment_enum,
    sentiment_score     NUMERIC(4,3),
    reputation_summary  TEXT,
    enriched_at         TIMESTAMPTZ,

    raw_html_key        TEXT,
    raw                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    scraped_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT builders_natural_key UNIQUE (source, source_id),
    CONSTRAINT builders_rating_chk CHECK (rating IS NULL OR rating BETWEEN 0 AND 5)
);

CREATE INDEX IF NOT EXISTS idx_builders_normalized  ON builders (normalized_name);
CREATE INDEX IF NOT EXISTS idx_builders_name_trgm   ON builders USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_builders_trust       ON builders (trust_score DESC NULLS LAST);

DROP TRIGGER IF EXISTS trg_builders_updated ON builders;
CREATE TRIGGER trg_builders_updated BEFORE UPDATE ON builders
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ===========================================================================
-- projects
-- ===========================================================================

CREATE TABLE IF NOT EXISTS projects (
    id                  BIGSERIAL PRIMARY KEY,
    source              source_enum NOT NULL,
    source_id           TEXT NOT NULL,
    project_url         TEXT NOT NULL,

    name                TEXT NOT NULL,
    normalized_name     TEXT NOT NULL,
    builder_id          BIGINT REFERENCES builders(id) ON DELETE SET NULL,
    builder_name        TEXT,
    location_id         BIGINT REFERENCES locations(id) ON DELETE SET NULL,

    status              possession_status_enum NOT NULL DEFAULT 'unknown',
    launch_date         DATE,
    possession_date     DATE,
    rera_number         TEXT,

    price_min           NUMERIC(16,2),
    price_max           NUMERIC(16,2),
    price_per_sqft      NUMERIC(14,2),
    total_units         INTEGER,
    total_towers        INTEGER,
    project_area_acres  DOUBLE PRECISION,

    configurations      JSONB NOT NULL DEFAULT '[]'::jsonb,
    amenities           TEXT[] NOT NULL DEFAULT '{}',
    specifications      JSONB NOT NULL DEFAULT '{}'::jsonb,
    landmarks           JSONB NOT NULL DEFAULT '[]'::jsonb,
    construction_updates JSONB NOT NULL DEFAULT '[]'::jsonb,
    description         TEXT,

    -- AI enrichment
    investment_score    NUMERIC(5,2),
    risk_score          NUMERIC(5,2),
    tags                TEXT[] NOT NULL DEFAULT '{}',
    enriched_at         TIMESTAMPTZ,

    raw_html_key        TEXT,
    raw                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    scraped_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT projects_natural_key UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_projects_builder     ON projects (builder_id);
CREATE INDEX IF NOT EXISTS idx_projects_location    ON projects (location_id);
CREATE INDEX IF NOT EXISTS idx_projects_status      ON projects (status);
CREATE INDEX IF NOT EXISTS idx_projects_rera        ON projects (rera_number) WHERE rera_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_projects_normalized  ON projects (normalized_name);
CREATE INDEX IF NOT EXISTS idx_projects_name_trgm   ON projects USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_projects_amenities   ON projects USING gin (amenities);

DROP TRIGGER IF EXISTS trg_projects_updated ON projects;
CREATE TRIGGER trg_projects_updated BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ===========================================================================
-- properties  (the fact table)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS properties (
    id                      BIGSERIAL PRIMARY KEY,
    source                  source_enum NOT NULL,
    source_id               TEXT NOT NULL,
    listing_url             TEXT NOT NULL,

    title                   TEXT,
    description             TEXT,
    project_id              BIGINT REFERENCES projects(id) ON DELETE SET NULL,
    project_name            TEXT,
    builder_id              BIGINT REFERENCES builders(id) ON DELETE SET NULL,
    builder_name            TEXT,
    developer_name          TEXT,
    society_name            TEXT,

    listing_type            listing_type_enum NOT NULL DEFAULT 'unknown',
    property_type           property_type_enum NOT NULL DEFAULT 'other',
    property_type_raw       TEXT,
    segment                 segment_enum NOT NULL DEFAULT 'unknown',
    is_commercial           BOOLEAN NOT NULL DEFAULT FALSE,
    is_luxury               BOOLEAN NOT NULL DEFAULT FALSE,
    is_affordable           BOOLEAN NOT NULL DEFAULT FALSE,

    configuration           TEXT,
    bedrooms                SMALLINT,
    bathrooms               SMALLINT,
    balconies               SMALLINT,
    floor_number            SMALLINT,
    total_floors            SMALLINT,
    facing                  TEXT,
    furnishing              TEXT,
    age_years               NUMERIC(5,1),

    price                   NUMERIC(16,2),
    price_max               NUMERIC(16,2),
    price_display           TEXT,
    price_per_sqft          NUMERIC(14,2),
    booking_amount          NUMERIC(16,2),
    maintenance_charge      NUMERIC(14,2),
    rent_monthly            NUMERIC(14,2),
    security_deposit        NUMERIC(16,2),
    is_price_on_request     BOOLEAN NOT NULL DEFAULT FALSE,

    area_value              DOUBLE PRECISION,
    area_unit               TEXT,
    area_sqft               DOUBLE PRECISION,
    carpet_area_sqft        DOUBLE PRECISION,
    built_up_area_sqft      DOUBLE PRECISION,
    super_built_up_area_sqft DOUBLE PRECISION,
    plot_area_sqft          DOUBLE PRECISION,

    location_id             BIGINT REFERENCES locations(id) ON DELETE SET NULL,
    location_raw            TEXT,
    city                    city_enum NOT NULL DEFAULT 'unknown',
    sector                  TEXT,
    locality                TEXT,
    micro_market            TEXT,
    latitude                DOUBLE PRECISION,
    longitude               DOUBLE PRECISION,

    possession_status       possession_status_enum NOT NULL DEFAULT 'unknown',
    possession_date         DATE,
    possession_raw          TEXT,
    rera_number             TEXT,
    rera_status             TEXT,
    total_units             INTEGER,
    project_area_acres      DOUBLE PRECISION,
    launch_date             DATE,

    amenities               TEXT[] NOT NULL DEFAULT '{}',
    specifications          JSONB NOT NULL DEFAULT '{}'::jsonb,
    unit_configurations     JSONB NOT NULL DEFAULT '[]'::jsonb,
    landmarks               JSONB NOT NULL DEFAULT '[]'::jsonb,

    contact_name            TEXT,
    contact_seller_type     seller_type_enum NOT NULL DEFAULT 'unknown',
    contact_company         TEXT,
    contact_phone           TEXT,
    contact_email           TEXT,

    listed_at               TIMESTAMPTZ,
    listing_date_raw        TEXT,
    updated_at_source       TIMESTAMPTZ,
    scraped_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    first_seen_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active               BOOLEAN NOT NULL DEFAULT TRUE,
    delisted_at             TIMESTAMPTZ,

    content_hash            TEXT,
    dedupe_key              TEXT,
    canonical_property_id   BIGINT REFERENCES properties(id) ON DELETE SET NULL,
    duplicate_count         SMALLINT NOT NULL DEFAULT 0,

    -- AI enrichment
    tags                    TEXT[] NOT NULL DEFAULT '{}',
    keywords                TEXT[] NOT NULL DEFAULT '{}',
    investment_score        NUMERIC(5,2),
    risk_score              NUMERIC(5,2),
    location_score          NUMERIC(5,2),
    builder_trust_score     NUMERIC(5,2),
    ai_summary              TEXT,
    enriched_at             TIMESTAMPTZ,
    enrichment_version      SMALLINT NOT NULL DEFAULT 0,

    raw_html_key            TEXT,
    raw                     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT properties_natural_key UNIQUE (source, source_id),
    CONSTRAINT properties_price_chk CHECK (price IS NULL OR price >= 0),
    CONSTRAINT properties_area_chk  CHECK (area_sqft IS NULL OR area_sqft > 0)
);

CREATE INDEX IF NOT EXISTS idx_properties_city_type    ON properties (city, listing_type, property_type);
CREATE INDEX IF NOT EXISTS idx_properties_sector       ON properties (city, sector);
CREATE INDEX IF NOT EXISTS idx_properties_micro_market ON properties (micro_market);
CREATE INDEX IF NOT EXISTS idx_properties_price        ON properties (price) WHERE price IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_properties_rent         ON properties (rent_monthly) WHERE rent_monthly IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_properties_ppsf         ON properties (price_per_sqft);
CREATE INDEX IF NOT EXISTS idx_properties_bedrooms     ON properties (bedrooms);
CREATE INDEX IF NOT EXISTS idx_properties_possession   ON properties (possession_status);
CREATE INDEX IF NOT EXISTS idx_properties_segment      ON properties (segment);
CREATE INDEX IF NOT EXISTS idx_properties_builder      ON properties (builder_id);
CREATE INDEX IF NOT EXISTS idx_properties_project      ON properties (project_id);
CREATE INDEX IF NOT EXISTS idx_properties_location     ON properties (location_id);
CREATE INDEX IF NOT EXISTS idx_properties_dedupe_key   ON properties (dedupe_key);
CREATE INDEX IF NOT EXISTS idx_properties_content_hash ON properties (content_hash);
CREATE INDEX IF NOT EXISTS idx_properties_active_seen  ON properties (is_active, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_properties_listed_at    ON properties (listed_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_properties_rera         ON properties (rera_number) WHERE rera_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_properties_amenities    ON properties USING gin (amenities);
CREATE INDEX IF NOT EXISTS idx_properties_tags         ON properties USING gin (tags);
CREATE INDEX IF NOT EXISTS idx_properties_needs_enrich ON properties (enriched_at NULLS FIRST)
    WHERE is_active;
-- Geo bounding-box queries without PostGIS.
CREATE INDEX IF NOT EXISTS idx_properties_geo          ON properties (latitude, longitude)
    WHERE latitude IS NOT NULL AND longitude IS NOT NULL;

DROP TRIGGER IF EXISTS trg_properties_updated ON properties;
CREATE TRIGGER trg_properties_updated BEFORE UPDATE ON properties
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ===========================================================================
-- property_images
-- ===========================================================================

CREATE TABLE IF NOT EXISTS property_images (
    id              BIGSERIAL PRIMARY KEY,
    property_id     BIGINT REFERENCES properties(id) ON DELETE CASCADE,
    project_id      BIGINT REFERENCES projects(id)   ON DELETE CASCADE,
    url             TEXT NOT NULL,
    url_hash        TEXT NOT NULL,
    caption         TEXT,
    is_primary      BOOLEAN NOT NULL DEFAULT FALSE,
    width           INTEGER,
    height          INTEGER,
    position        SMALLINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT property_images_owner_chk
        CHECK (property_id IS NOT NULL OR project_id IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_property_images_prop
    ON property_images (property_id, url_hash) WHERE property_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_property_images_proj
    ON property_images (project_id, url_hash) WHERE project_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_property_images_property ON property_images (property_id);

-- ===========================================================================
-- price_history  (append-only, trigger-driven)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS price_history (
    id                  BIGSERIAL PRIMARY KEY,
    property_id         BIGINT REFERENCES properties(id) ON DELETE CASCADE,
    project_id          BIGINT REFERENCES projects(id)   ON DELETE CASCADE,
    observed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    price               NUMERIC(16,2),
    price_per_sqft      NUMERIC(14,2),
    rent_monthly        NUMERIC(14,2),
    previous_price      NUMERIC(16,2),
    change_amount       NUMERIC(16,2),
    change_pct          NUMERIC(8,3),
    source              source_enum NOT NULL,

    CONSTRAINT price_history_owner_chk
        CHECK (property_id IS NOT NULL OR project_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_price_history_property ON price_history (property_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_price_history_project  ON price_history (project_id,  observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_price_history_observed ON price_history (observed_at DESC);

-- Capture every price movement automatically. Application code cannot forget.
CREATE OR REPLACE FUNCTION record_price_change() RETURNS TRIGGER AS $$
DECLARE
    old_price NUMERIC(16,2);
    new_price NUMERIC(16,2);
BEGIN
    old_price := COALESCE(OLD.price, OLD.rent_monthly);
    new_price := COALESCE(NEW.price, NEW.rent_monthly);

    IF new_price IS NOT NULL AND (old_price IS DISTINCT FROM new_price) THEN
        INSERT INTO price_history (
            property_id, observed_at, price, price_per_sqft, rent_monthly,
            previous_price, change_amount, change_pct, source
        ) VALUES (
            NEW.id, NOW(), NEW.price, NEW.price_per_sqft, NEW.rent_monthly,
            old_price,
            CASE WHEN old_price IS NULL THEN NULL ELSE new_price - old_price END,
            CASE WHEN old_price IS NULL OR old_price = 0 THEN NULL
                 ELSE ROUND(((new_price - old_price) / old_price) * 100, 3) END,
            NEW.source
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_properties_price_history ON properties;
CREATE TRIGGER trg_properties_price_history
    AFTER UPDATE OF price, rent_monthly ON properties
    FOR EACH ROW EXECUTE FUNCTION record_price_change();

-- Seed the first observation on insert so a listing always has ≥1 data point.
CREATE OR REPLACE FUNCTION seed_price_history() RETURNS TRIGGER AS $$
BEGIN
    IF COALESCE(NEW.price, NEW.rent_monthly) IS NOT NULL THEN
        INSERT INTO price_history (property_id, observed_at, price, price_per_sqft,
                                   rent_monthly, source)
        VALUES (NEW.id, NOW(), NEW.price, NEW.price_per_sqft, NEW.rent_monthly, NEW.source);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_properties_price_seed ON properties;
CREATE TRIGGER trg_properties_price_seed
    AFTER INSERT ON properties
    FOR EACH ROW EXECUTE FUNCTION seed_price_history();

-- ===========================================================================
-- reddit
-- ===========================================================================

CREATE TABLE IF NOT EXISTS reddit_posts (
    id                  BIGSERIAL PRIMARY KEY,
    source_id           TEXT NOT NULL UNIQUE,      -- reddit t3 id, e.g. "1abc2de"
    subreddit           TEXT NOT NULL,
    url                 TEXT NOT NULL,
    permalink           TEXT NOT NULL,

    title               TEXT NOT NULL,
    body                TEXT,
    author              TEXT,
    created_utc         TIMESTAMPTZ,
    score               INTEGER NOT NULL DEFAULT 0,
    upvote_ratio        NUMERIC(4,3),
    num_comments        INTEGER NOT NULL DEFAULT 0,
    flair               TEXT,
    is_self             BOOLEAN NOT NULL DEFAULT TRUE,
    over_18             BOOLEAN NOT NULL DEFAULT FALSE,

    -- enrichment
    sentiment           sentiment_enum,
    sentiment_score     NUMERIC(4,3),
    detected_builders   TEXT[] NOT NULL DEFAULT '{}',
    detected_projects   TEXT[] NOT NULL DEFAULT '{}',
    detected_sectors    TEXT[] NOT NULL DEFAULT '{}',
    detected_city       city_enum NOT NULL DEFAULT 'unknown',
    topics              TEXT[] NOT NULL DEFAULT '{}',
    keywords            TEXT[] NOT NULL DEFAULT '{}',
    summary             TEXT,
    relevance_score     NUMERIC(5,2),
    enriched_at         TIMESTAMPTZ,

    raw                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    scraped_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reddit_posts_subreddit ON reddit_posts (subreddit, created_utc DESC);
CREATE INDEX IF NOT EXISTS idx_reddit_posts_created   ON reddit_posts (created_utc DESC);
CREATE INDEX IF NOT EXISTS idx_reddit_posts_score     ON reddit_posts (score DESC);
CREATE INDEX IF NOT EXISTS idx_reddit_posts_builders  ON reddit_posts USING gin (detected_builders);
CREATE INDEX IF NOT EXISTS idx_reddit_posts_projects  ON reddit_posts USING gin (detected_projects);
CREATE INDEX IF NOT EXISTS idx_reddit_posts_sectors   ON reddit_posts USING gin (detected_sectors);
CREATE INDEX IF NOT EXISTS idx_reddit_posts_topics    ON reddit_posts USING gin (topics);
CREATE INDEX IF NOT EXISTS idx_reddit_posts_sentiment ON reddit_posts (sentiment);
CREATE INDEX IF NOT EXISTS idx_reddit_posts_enrich    ON reddit_posts (enriched_at NULLS FIRST);

DROP TRIGGER IF EXISTS trg_reddit_posts_updated ON reddit_posts;
CREATE TRIGGER trg_reddit_posts_updated BEFORE UPDATE ON reddit_posts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS reddit_comments (
    id                  BIGSERIAL PRIMARY KEY,
    comment_id          TEXT NOT NULL UNIQUE,
    post_id             BIGINT NOT NULL REFERENCES reddit_posts(id) ON DELETE CASCADE,
    post_source_id      TEXT NOT NULL,
    parent_id           TEXT,
    author              TEXT,
    body                TEXT,
    score               INTEGER NOT NULL DEFAULT 0,
    depth               SMALLINT NOT NULL DEFAULT 0,
    is_submitter        BOOLEAN NOT NULL DEFAULT FALSE,
    created_utc         TIMESTAMPTZ,
    permalink           TEXT,

    sentiment           sentiment_enum,
    sentiment_score     NUMERIC(4,3),
    detected_builders   TEXT[] NOT NULL DEFAULT '{}',
    detected_projects   TEXT[] NOT NULL DEFAULT '{}',
    detected_sectors    TEXT[] NOT NULL DEFAULT '{}',
    topics              TEXT[] NOT NULL DEFAULT '{}',
    keywords            TEXT[] NOT NULL DEFAULT '{}',
    enriched_at         TIMESTAMPTZ,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reddit_comments_post      ON reddit_comments (post_id);
CREATE INDEX IF NOT EXISTS idx_reddit_comments_score     ON reddit_comments (score DESC);
CREATE INDEX IF NOT EXISTS idx_reddit_comments_builders  ON reddit_comments USING gin (detected_builders);
CREATE INDEX IF NOT EXISTS idx_reddit_comments_sentiment ON reddit_comments (sentiment);

-- ===========================================================================
-- market_insights
-- ===========================================================================

CREATE TABLE IF NOT EXISTS market_insights (
    id                  BIGSERIAL PRIMARY KEY,
    source              source_enum NOT NULL,
    source_id           TEXT NOT NULL,
    metric              TEXT NOT NULL,
    city                city_enum NOT NULL DEFAULT 'unknown',
    locality            TEXT,
    sector              TEXT,
    location_id         BIGINT REFERENCES locations(id) ON DELETE SET NULL,
    property_type       property_type_enum,
    period_start        DATE,
    period_end          DATE,
    value               NUMERIC(18,4),
    unit                TEXT,
    change_pct          NUMERIC(8,3),
    sample_size         INTEGER,
    source_url          TEXT,
    notes               TEXT,
    raw                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    scraped_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT market_insights_natural_key UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_market_insights_lookup
    ON market_insights (metric, city, sector, period_start DESC);

-- ===========================================================================
-- operational tables
-- ===========================================================================

CREATE TABLE IF NOT EXISTS scrape_state (
    source          TEXT NOT NULL,
    job             TEXT NOT NULL,
    cursor          JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_run_at     TIMESTAMPTZ,
    seen_hashes     TEXT[] NOT NULL DEFAULT '{}',
    stats           JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, job)
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id              BIGSERIAL PRIMARY KEY,
    run_id          UUID NOT NULL DEFAULT uuid_generate_v4(),
    source          TEXT NOT NULL,
    job             TEXT NOT NULL,
    status          job_status_enum NOT NULL DEFAULT 'running',
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    duration_s      NUMERIC(10,2),
    discovered      INTEGER NOT NULL DEFAULT 0,
    fetched         INTEGER NOT NULL DEFAULT 0,
    parsed          INTEGER NOT NULL DEFAULT 0,
    inserted        INTEGER NOT NULL DEFAULT 0,
    updated         INTEGER NOT NULL DEFAULT 0,
    skipped_known   INTEGER NOT NULL DEFAULT 0,
    skipped_robots  INTEGER NOT NULL DEFAULT 0,
    errors          INTEGER NOT NULL DEFAULT 0,
    blocked         INTEGER NOT NULL DEFAULT 0,
    details         JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_scrape_runs_source ON scrape_runs (source, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_scrape_runs_status ON scrape_runs (status, started_at DESC);

-- Cross-source duplicate links discovered by the dedupe pass.
CREATE TABLE IF NOT EXISTS property_duplicates (
    id                  BIGSERIAL PRIMARY KEY,
    canonical_id        BIGINT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    duplicate_id        BIGINT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    score               NUMERIC(4,3) NOT NULL,
    reason              TEXT,
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT property_duplicates_pair UNIQUE (canonical_id, duplicate_id),
    CONSTRAINT property_duplicates_not_self CHECK (canonical_id <> duplicate_id)
);

-- Queue for LLM enrichment work that failed or is pending.
CREATE TABLE IF NOT EXISTS enrichment_queue (
    id              BIGSERIAL PRIMARY KEY,
    entity_type     TEXT NOT NULL,        -- property | project | builder | reddit_post
    entity_id       BIGINT NOT NULL,
    priority        SMALLINT NOT NULL DEFAULT 5,
    attempts        SMALLINT NOT NULL DEFAULT 0,
    last_error      TEXT,
    batch_id        TEXT,
    enqueued_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at    TIMESTAMPTZ,
    CONSTRAINT enrichment_queue_entity UNIQUE (entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_enrichment_queue_pending
    ON enrichment_queue (priority, enqueued_at) WHERE processed_at IS NULL;
