-- WoW Forever Local Telemetry & Database Schema (SQLite, WAL mode)
-- Idempotent: safe to re-run at any time.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ah_houses (
    region          TEXT NOT NULL,
    ruleset_id      INTEGER NOT NULL,
    ruleset_name    TEXT NOT NULL DEFAULT '',
    house_id        INTEGER NOT NULL,
    house_name      TEXT NOT NULL DEFAULT '',
    discovered_at   TEXT NOT NULL,
    PRIMARY KEY (region, ruleset_id, house_id)
);

CREATE TABLE IF NOT EXISTS ah_snapshots (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    region          TEXT NOT NULL,
    ruleset_id      INTEGER NOT NULL,
    house_id        INTEGER NOT NULL,
    endpoint_type   TEXT NOT NULL CHECK (endpoint_type IN ('auctions', 'commodities')),
    last_modified   TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    raw_count       INTEGER NOT NULL DEFAULT 0,
    unique_items    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (region, ruleset_id, house_id, endpoint_type, last_modified)
);

CREATE INDEX IF NOT EXISTS idx_ah_snapshots_lookup
    ON ah_snapshots (region, ruleset_id, house_id, endpoint_type, fetched_at DESC);

CREATE TABLE IF NOT EXISTS ah_item_prices (
    snapshot_id     INTEGER NOT NULL REFERENCES ah_snapshots(snapshot_id) ON DELETE CASCADE,
    item_id         INTEGER NOT NULL,
    min_buyout      INTEGER NOT NULL,   -- copper
    p10_price       INTEGER NOT NULL,   -- 10th-percentile buyout (copper), resistant to 1c bait listings
    median_price    INTEGER NOT NULL,   -- copper
    mean_price      INTEGER NOT NULL,   -- copper
    total_qty       INTEGER NOT NULL,
    listing_count   INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_ah_item_prices_item
    ON ah_item_prices (item_id, snapshot_id DESC);

-- Latest snapshot per (region, ruleset_id, house_id, endpoint_type)
CREATE VIEW IF NOT EXISTS ah_latest_snapshots AS
SELECT s.*
FROM ah_snapshots s
INNER JOIN (
    SELECT region, ruleset_id, house_id, endpoint_type, MAX(snapshot_id) AS max_id
    FROM ah_snapshots
    GROUP BY region, ruleset_id, house_id, endpoint_type
) latest ON s.snapshot_id = latest.max_id;

-- Latest live prices across both auctions and commodities
CREATE VIEW IF NOT EXISTS ah_latest_prices AS
SELECT
    s.region,
    s.ruleset_id,
    s.house_id,
    s.endpoint_type,
    s.last_modified,
    s.fetched_at,
    p.item_id,
    p.min_buyout,
    p.p10_price,
    p.median_price,
    p.mean_price,
    p.total_qty,
    p.listing_count
FROM ah_item_prices p
INNER JOIN ah_latest_snapshots s ON p.snapshot_id = s.snapshot_id;

-- Rolling 7-day market statistics per item
CREATE VIEW IF NOT EXISTS ah_rolling_7d AS
SELECT
    s.region,
    s.ruleset_id,
    s.house_id,
    p.item_id,
    COUNT(*)                        AS snapshots_seen,
    MIN(p.min_buyout)               AS min_7d,
    CAST(AVG(p.p10_price) AS INT)   AS avg_p10_7d,
    CAST(AVG(p.median_price) AS INT) AS avg_median_7d,
    CAST(AVG(p.total_qty) AS INT)   AS avg_qty_7d,
    MAX(s.fetched_at)               AS last_seen
FROM ah_item_prices p
INNER JOIN ah_snapshots s ON p.snapshot_id = s.snapshot_id
WHERE s.fetched_at >= datetime('now', '-7 days')
GROUP BY s.region, s.ruleset_id, s.house_id, p.item_id;

-- =====================================================================
-- Stage 2: WoW Forever Static Catalog (DB2) + Classic World Loot Tables
-- =====================================================================

CREATE TABLE IF NOT EXISTS db_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_classes (
    class_id    INTEGER PRIMARY KEY,
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_subclasses (
    class_id    INTEGER NOT NULL,
    subclass_id INTEGER NOT NULL,
    name        TEXT NOT NULL,
    PRIMARY KEY (class_id, subclass_id)
);

CREATE TABLE IF NOT EXISTS items (
    item_id         INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    quality         INTEGER NOT NULL DEFAULT 0,
    item_level      INTEGER NOT NULL DEFAULT 0,
    required_level  INTEGER NOT NULL DEFAULT 0,
    class_id        INTEGER NOT NULL DEFAULT -1,
    subclass_id     INTEGER NOT NULL DEFAULT -1,
    sell_price      INTEGER NOT NULL DEFAULT 0,   -- vendor sell price in copper
    buy_price       INTEGER NOT NULL DEFAULT 0,
    stackable       INTEGER NOT NULL DEFAULT 1,
    bonding         INTEGER NOT NULL DEFAULT 0,   -- 0=none, 1=BoP, 2=BoE, 3=BoU, 4=Quest
    inventory_type  INTEGER NOT NULL DEFAULT 0,
    description     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_items_name ON items (name);
CREATE INDEX IF NOT EXISTS idx_items_class ON items (class_id, subclass_id);

CREATE TABLE IF NOT EXISTS areas (
    area_id             INTEGER PRIMARY KEY,
    continent_id        INTEGER NOT NULL DEFAULT 0,
    parent_area_id      INTEGER NOT NULL DEFAULT 0,
    name                TEXT NOT NULL,
    exploration_level   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS creatures (
    entry               INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    subname             TEXT NOT NULL DEFAULT '',
    min_level           INTEGER NOT NULL DEFAULT 0,
    max_level           INTEGER NOT NULL DEFAULT 0,
    rank                INTEGER NOT NULL DEFAULT 0,   -- 0=Normal, 1=Elite, 2=RareElite, 3=Boss, 4=Rare
    creature_type       INTEGER NOT NULL DEFAULT 0,   -- 1=Beast,2=Dragon,3=Demon,4=Elem,5=Giant,6=Undead,7=Humanoid
    faction             INTEGER NOT NULL DEFAULT 0,
    min_loot_gold       INTEGER NOT NULL DEFAULT 0,
    max_loot_gold       INTEGER NOT NULL DEFAULT 0,
    loot_id             INTEGER NOT NULL DEFAULT 0,
    skinning_loot_id    INTEGER NOT NULL DEFAULT 0,
    spawn_count         INTEGER NOT NULL DEFAULT 0,
    primary_map         INTEGER NOT NULL DEFAULT -1
);

CREATE INDEX IF NOT EXISTS idx_creatures_level_type ON creatures (max_level, rank, creature_type);
CREATE INDEX IF NOT EXISTS idx_creatures_loot_id ON creatures (loot_id);

CREATE TABLE IF NOT EXISTS creature_loot (
    loot_id     INTEGER NOT NULL,
    item_id     INTEGER NOT NULL,   -- item_id (or reference_loot.ref_id when is_reference=1)
    chance      REAL NOT NULL,      -- 0..100
    group_id    INTEGER NOT NULL DEFAULT 0,
    min_count   INTEGER NOT NULL DEFAULT 1,
    max_count   INTEGER NOT NULL DEFAULT 1,
    is_reference INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_creature_loot_lid ON creature_loot (loot_id);
CREATE INDEX IF NOT EXISTS idx_creature_loot_item ON creature_loot (item_id, is_reference);

CREATE TABLE IF NOT EXISTS reference_loot (
    ref_id      INTEGER NOT NULL,
    item_id     INTEGER NOT NULL,
    chance      REAL NOT NULL,
    group_id    INTEGER NOT NULL DEFAULT 0,
    min_count   INTEGER NOT NULL DEFAULT 1,
    max_count   INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_reference_loot_ref ON reference_loot (ref_id);

-- Effective unit value (copper) for each item:
-- For non-BoP items with live AH listings, take MAX(vendor_sell, 95% of AH p10_price).
-- Otherwise fall back to vendor sell_price.
CREATE VIEW IF NOT EXISTS v_item_value AS
WITH latest_ah AS (
    SELECT
        item_id,
        MIN(p10_price) AS p10_price,
        MIN(min_buyout) AS min_buyout,
        SUM(total_qty) AS total_qty,
        SUM(listing_count) AS listing_count
    FROM ah_latest_prices
    GROUP BY item_id
)
SELECT
    i.item_id,
    i.name,
    i.quality,
    i.class_id,
    i.subclass_id,
    i.bonding,
    i.sell_price,
    COALESCE(a.min_buyout, 0) AS ah_min_buyout,
    COALESCE(a.p10_price, 0)  AS ah_p10_price,
    COALESCE(a.total_qty, 0)  AS ah_total_qty,
    COALESCE(a.listing_count, 0) AS ah_listing_count,
    CASE
        WHEN i.bonding NOT IN (1, 4) AND COALESCE(a.p10_price, 0) > 0
             AND CAST(a.p10_price * 0.95 AS INT) > i.sell_price
        THEN CAST(a.p10_price * 0.95 AS INT)
        ELSE i.sell_price
    END AS effective_copper
FROM items i
LEFT JOIN latest_ah a ON a.item_id = i.item_id;

