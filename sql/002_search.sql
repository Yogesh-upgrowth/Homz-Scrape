-- ===========================================================================
-- Search surface: full-text vectors, trigram indexes, materialized rollups.
-- Run after 001_schema.sql. Idempotent.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- Immutable array→text helper.
--
-- Postgres marks the built-in array_to_string() STABLE (its volatility is
-- derived conservatively from the element output function), so it cannot be
-- used inside a GENERATED ... STORED expression — the DDL fails with
-- "generation expression is not immutable".
--
-- text[] specifically is safe: text's output function is a no-op. This wrapper
-- asserts that, restricted to text[] so the guarantee actually holds.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION immutable_array_to_string(arr text[], sep text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$ SELECT array_to_string(arr, sep) $$;

-- ---------------------------------------------------------------------------
-- properties: one tsvector covering everything a user might type
-- ---------------------------------------------------------------------------

ALTER TABLE properties
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(project_name, '')),  'A') ||
        setweight(to_tsvector('english', coalesce(builder_name, '')),  'A') ||
        setweight(to_tsvector('english', coalesce(society_name, '')),  'A') ||
        setweight(to_tsvector('english', coalesce(sector, '')),        'B') ||
        setweight(to_tsvector('english', coalesce(locality, '')),      'B') ||
        setweight(to_tsvector('english', coalesce(micro_market, '')),  'B') ||
        setweight(to_tsvector('english', coalesce(configuration, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(title, '')),         'C') ||
        setweight(to_tsvector('english',
            coalesce(immutable_array_to_string(amenities, ' '), '')),  'D') ||
        setweight(to_tsvector('english', left(coalesce(description, ''), 8000)), 'D')
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_properties_search ON properties USING gin (search_vector);
CREATE INDEX IF NOT EXISTS idx_properties_project_name_trgm
    ON properties USING gin (project_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_properties_builder_name_trgm
    ON properties USING gin (builder_name gin_trgm_ops);

-- The hot filter path: city + type + price, restricted to live listings.
CREATE INDEX IF NOT EXISTS idx_properties_search_filters
    ON properties (city, listing_type, property_type, price)
    WHERE is_active AND canonical_property_id IS NULL;

-- ---------------------------------------------------------------------------
-- projects & builders full text
-- ---------------------------------------------------------------------------

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(name, '')),         'A') ||
        setweight(to_tsvector('english', coalesce(builder_name, '')), 'A') ||
        setweight(to_tsvector('english',
            coalesce(immutable_array_to_string(amenities, ' '), '')), 'C') ||
        setweight(to_tsvector('english', left(coalesce(description, ''), 8000)), 'D')
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_projects_search ON projects USING gin (search_vector);

ALTER TABLE builders
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(name, '')), 'A') ||
        setweight(to_tsvector('english', left(coalesce(description, ''), 8000)), 'D')
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_builders_search ON builders USING gin (search_vector);

-- ---------------------------------------------------------------------------
-- reddit full text
-- ---------------------------------------------------------------------------

ALTER TABLE reddit_posts
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english',
            coalesce(immutable_array_to_string(detected_builders, ' '), '')), 'A') ||
        setweight(to_tsvector('english',
            coalesce(immutable_array_to_string(detected_projects, ' '), '')), 'A') ||
        setweight(to_tsvector('english',
            coalesce(immutable_array_to_string(detected_sectors, ' '), '')), 'B') ||
        setweight(to_tsvector('english',
            coalesce(immutable_array_to_string(topics, ' '), '')), 'B') ||
        setweight(to_tsvector('english', left(coalesce(body, ''), 12000)), 'C')
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_reddit_posts_search ON reddit_posts USING gin (search_vector);

ALTER TABLE reddit_comments
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (to_tsvector('english', left(coalesce(body, ''), 12000))) STORED;

CREATE INDEX IF NOT EXISTS idx_reddit_comments_search
    ON reddit_comments USING gin (search_vector);

-- ===========================================================================
-- Materialized rollups
-- ===========================================================================

-- Price / supply per (city, sector, property_type, listing_type, month).
DROP MATERIALIZED VIEW IF EXISTS mv_locality_price_trends CASCADE;
CREATE MATERIALIZED VIEW mv_locality_price_trends AS
SELECT
    p.city,
    p.sector,
    p.micro_market,
    p.property_type,
    p.listing_type,
    date_trunc('month', COALESCE(p.listed_at, p.first_seen_at))::date AS period,
    COUNT(*)                                        AS listing_count,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY p.price_per_sqft)
        FILTER (WHERE p.price_per_sqft > 0)         AS median_price_per_sqft,
    AVG(p.price_per_sqft) FILTER (WHERE p.price_per_sqft > 0) AS avg_price_per_sqft,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY p.price)
        FILTER (WHERE p.price > 0)                  AS median_price,
    MIN(p.price) FILTER (WHERE p.price > 0)         AS min_price,
    MAX(p.price) FILTER (WHERE p.price > 0)         AS max_price,
    AVG(p.rent_monthly) FILTER (WHERE p.rent_monthly > 0) AS avg_rent,
    AVG(p.area_sqft) FILTER (WHERE p.area_sqft > 0) AS avg_area_sqft
FROM properties p
WHERE p.is_active AND p.canonical_property_id IS NULL
GROUP BY 1,2,3,4,5,6;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mv_locality_price_trends
    ON mv_locality_price_trends (city, COALESCE(sector,''), COALESCE(micro_market,''),
                                 property_type, listing_type, period);

-- Rental yield: median annual rent ÷ median sale price for the same locality
-- and configuration. Only emitted where both sides have real sample size.
DROP MATERIALIZED VIEW IF EXISTS mv_rental_yield CASCADE;
CREATE MATERIALIZED VIEW mv_rental_yield AS
WITH sale AS (
    SELECT city, sector, bedrooms,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY price) AS median_price,
           COUNT(*) AS n
    FROM properties
    WHERE is_active AND listing_type IN ('sale','resale','new_launch')
      AND price > 0 AND bedrooms IS NOT NULL
    GROUP BY 1,2,3
), rent AS (
    SELECT city, sector, bedrooms,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY rent_monthly) AS median_rent,
           COUNT(*) AS n
    FROM properties
    WHERE is_active AND listing_type = 'rent'
      AND rent_monthly > 0 AND bedrooms IS NOT NULL
    GROUP BY 1,2,3
)
SELECT s.city, s.sector, s.bedrooms,
       s.median_price, r.median_rent,
       ROUND(((r.median_rent * 12) / NULLIF(s.median_price, 0) * 100)::numeric, 3)
           AS rental_yield_pct,
       s.n AS sale_sample, r.n AS rent_sample
FROM sale s
JOIN rent r USING (city, sector, bedrooms)
WHERE s.n >= 3 AND r.n >= 3;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mv_rental_yield
    ON mv_rental_yield (city, COALESCE(sector,''), bedrooms);

-- Builder scorecard: portfolio size + Reddit sentiment in one row.
DROP MATERIALIZED VIEW IF EXISTS mv_builder_scorecard CASCADE;
CREATE MATERIALIZED VIEW mv_builder_scorecard AS
WITH portfolio AS (
    SELECT b.id AS builder_id, b.name, b.normalized_name,
           COUNT(DISTINCT pr.id)                                        AS project_count,
           COUNT(DISTINCT pr.id) FILTER (WHERE pr.status = 'completed') AS completed_count,
           COUNT(DISTINCT pr.id) FILTER (WHERE pr.status = 'under_construction')
                                                                        AS ongoing_count,
           COUNT(DISTINCT p.id)                                         AS listing_count,
           AVG(p.price_per_sqft) FILTER (WHERE p.price_per_sqft > 0)    AS avg_price_per_sqft
    FROM builders b
    LEFT JOIN projects   pr ON pr.builder_id = b.id
    LEFT JOIN properties p  ON p.builder_id  = b.id AND p.is_active
    GROUP BY b.id, b.name, b.normalized_name
), chatter AS (
    SELECT lower(unnest(detected_builders)) AS builder_label,
           COUNT(*)                                                     AS mention_count,
           COUNT(*) FILTER (WHERE sentiment = 'positive')               AS positive_count,
           COUNT(*) FILTER (WHERE sentiment = 'negative')               AS negative_count,
           AVG(sentiment_score)                                         AS avg_sentiment
    FROM reddit_posts
    WHERE detected_builders <> '{}'
    GROUP BY 1
)
SELECT p.builder_id, p.name, p.normalized_name,
       p.project_count, p.completed_count, p.ongoing_count,
       p.listing_count, p.avg_price_per_sqft,
       COALESCE(c.mention_count, 0)  AS reddit_mentions,
       COALESCE(c.positive_count, 0) AS reddit_positive,
       COALESCE(c.negative_count, 0) AS reddit_negative,
       c.avg_sentiment               AS reddit_avg_sentiment
FROM portfolio p
LEFT JOIN chatter c ON c.builder_label = lower(p.name);

CREATE UNIQUE INDEX IF NOT EXISTS uq_mv_builder_scorecard ON mv_builder_scorecard (builder_id);

-- Supply/demand snapshot per locality, last 90 days.
DROP MATERIALIZED VIEW IF EXISTS mv_supply_demand CASCADE;
CREATE MATERIALIZED VIEW mv_supply_demand AS
SELECT
    city, sector,
    COUNT(*) FILTER (WHERE first_seen_at > NOW() - INTERVAL '30 days')  AS new_last_30d,
    COUNT(*) FILTER (WHERE first_seen_at > NOW() - INTERVAL '90 days')  AS new_last_90d,
    COUNT(*) FILTER (WHERE NOT is_active
                       AND delisted_at > NOW() - INTERVAL '90 days')    AS delisted_last_90d,
    COUNT(*) FILTER (WHERE is_active)                                   AS active_supply,
    COUNT(*) FILTER (WHERE is_active AND possession_status = 'new_launch') AS new_launches,
    AVG(EXTRACT(EPOCH FROM (COALESCE(delisted_at, NOW()) - first_seen_at)) / 86400)
        FILTER (WHERE NOT is_active)                                    AS avg_days_on_market
FROM properties
WHERE canonical_property_id IS NULL
GROUP BY city, sector;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mv_supply_demand
    ON mv_supply_demand (city, COALESCE(sector,''));

-- ---------------------------------------------------------------------------
-- refresh helper — called by the ETL job / cron
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION refresh_market_views(concurrent BOOLEAN DEFAULT TRUE)
RETURNS void AS $$
BEGIN
    IF concurrent THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY mv_locality_price_trends;
        REFRESH MATERIALIZED VIEW CONCURRENTLY mv_rental_yield;
        REFRESH MATERIALIZED VIEW CONCURRENTLY mv_builder_scorecard;
        REFRESH MATERIALIZED VIEW CONCURRENTLY mv_supply_demand;
    ELSE
        REFRESH MATERIALIZED VIEW mv_locality_price_trends;
        REFRESH MATERIALIZED VIEW mv_rental_yield;
        REFRESH MATERIALIZED VIEW mv_builder_scorecard;
        REFRESH MATERIALIZED VIEW mv_supply_demand;
    END IF;
END;
$$ LANGUAGE plpgsql;
