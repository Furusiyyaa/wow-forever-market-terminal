#!/usr/bin/env python3
"""
WoW Forever Auction House Telemetry Poller (Stage 1)
----------------------------------------------------
Polls Blizzard's official WoW Forever Game Data API (dynamic-forever-{region})
using OAuth2 client_credentials and stores hourly market snapshots in a local
SQLite database (work/telemetry/wow_forever.db).

Zero background daemon overhead (SQLite WAL mode). Fully idempotent.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "telemetry" / "schema.sql"
DEFAULT_DB_PATH = REPO_ROOT / "work" / "telemetry" / "wow_forever.db"
ENV_PATH = REPO_ROOT / "telemetry" / ".env"

OAUTH_TOKEN_URL = "https://oauth.battle.net/token"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_copper(copper: int) -> str:
    g = copper // 10000
    s = (copper % 10000) // 100
    c = copper % 100
    return f"{g}g {s:02d}s {c:02d}c"


def load_config() -> dict[str, str]:
    load_dotenv(ENV_PATH)
    load_dotenv(REPO_ROOT / ".env")
    return {
        "client_id": os.getenv("BLIZZARD_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("BLIZZARD_CLIENT_SECRET", "").strip(),
        "region": os.getenv("BLIZZARD_REGION", "eu").strip().lower(),
        "ruleset_id": os.getenv("FOREVER_RULESET_ID", "").strip(),
        "house_id": os.getenv("FOREVER_HOUSE_ID", "").strip(),
    }


def require_credentials(cfg: dict[str, str]) -> tuple[str, str]:
    if not cfg["client_id"] or not cfg["client_secret"]:
        print(
            "ERROR: Missing BLIZZARD_CLIENT_ID or BLIZZARD_CLIENT_SECRET.\n"
            f"Copy telemetry/.env.example to {ENV_PATH} and add your free Battle.net API credentials\n"
            "from https://develop.battle.net/access/clients",
            file=sys.stderr,
        )
        sys.exit(2)
    return cfg["client_id"], cfg["client_secret"]


def open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Path) -> None:
    if not SCHEMA_PATH.exists():
        print(f"ERROR: Schema file not found at {SCHEMA_PATH}", file=sys.stderr)
        sys.exit(1)
    conn = open_db(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


def get_oauth_token(client: httpx.Client, client_id: str, client_secret: str) -> str:
    resp = client.post(
        OAUTH_TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=20.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"OAuth token request failed ({resp.status_code}): {resp.text[:300]}"
        )
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"OAuth response missing access_token: {data}")
    return token


def api_base(region: str) -> str:
    return f"https://{region}.api.blizzard.com"


def api_namespace(region: str) -> str:
    return f"dynamic-forever-{region}"


def extract_localized(val: Any) -> str:
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return str(val.get("en_US") or val.get("en_GB") or next(iter(val.values()), ""))
    return ""


def cmd_init(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    init_db(db_path)
    print(f"OK: Initialized SQLite database at {db_path}")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    cfg = load_config()
    client_id, client_secret = require_credentials(cfg)
    region = args.region or cfg["region"]
    db_path = Path(args.db)
    init_db(db_path)

    ns = api_namespace(region)
    base = api_base(region)
    headers_base = {"Battlenet-Namespace": ns}

    with httpx.Client(timeout=30.0) as client:
        token = get_oauth_token(client, client_id, client_secret)
        headers = {**headers_base, "Authorization": f"Bearer {token}"}

        # 1. Discover rulesets
        ruleset_url = f"{base}/data/wow/ruleset/index"
        r = client.get(ruleset_url, headers=headers, params={"namespace": ns, "locale": "en_US"})
        if r.status_code != 200:
            print(
                f"ERROR: GET {ruleset_url} returned {r.status_code}: {r.text[:400]}",
                file=sys.stderr,
            )
            return 1

        payload = r.json()
        rulesets = payload.get("rulesets") or payload.get("items") or []
        if not rulesets:
            print("WARNING: No rulesets returned. Raw payload:")
            print(json.dumps(payload, indent=2)[:1500])
            return 1

        discovered: list[tuple[str, int, str, int, str]] = []
        now = utc_now_iso()

        for rs in rulesets:
            rs_id = rs.get("id")
            if rs_id is None and isinstance(rs.get("key"), dict):
                href = rs["key"].get("href", "")
                parts = [p for p in href.split("?")[0].split("/") if p.isdigit()]
                rs_id = int(parts[-1]) if parts else None
            if rs_id is None:
                continue
            rs_name = extract_localized(rs.get("name")) or f"Ruleset {rs_id}"

            # 2. Discover auction houses for this ruleset (handle trailing slash quirk)
            ah_resp = None
            for path in (
                f"{base}/data/wow/ruleset/{rs_id}/auctions/index",
                f"{base}/data/wow/ruleset/{rs_id}/auctions/",
            ):
                resp = client.get(path, headers=headers, params={"namespace": ns, "locale": "en_US"})
                if resp.status_code == 200:
                    ah_resp = resp.json()
                    break

            if not ah_resp:
                print(f"  [ruleset {rs_id} - {rs_name}] No auction house index found")
                continue

            houses = ah_resp.get("auctions") or ah_resp.get("auction_houses") or []
            for h in houses:
                h_id = h.get("id")
                h_name = extract_localized(h.get("name")) or f"House {h_id}"
                if h_id is not None:
                    discovered.append((region, int(rs_id), rs_name, int(h_id), h_name))

        conn = open_db(db_path)
        try:
            conn.executemany(
                """
                INSERT INTO ah_houses (region, ruleset_id, ruleset_name, house_id, house_name, discovered_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (region, ruleset_id, house_id) DO UPDATE SET
                    ruleset_name = excluded.ruleset_name,
                    house_name   = excluded.house_name,
                    discovered_at = excluded.discovered_at
                """,
                [(rg, rid, rname, hid, hname, now) for (rg, rid, rname, hid, hname) in discovered],
            )
            conn.commit()
        finally:
            conn.close()

    print(f"Discovered {len(discovered)} auction house(s) in {ns}:")
    for rg, rid, rname, hid, hname in discovered:
        print(f"  region={rg}  ruleset_id={rid:<4} ({rname})  house_id={hid:<4} ({hname})")
    print("\nSet FOREVER_RULESET_ID and FOREVER_HOUSE_ID in telemetry/.env, then run: poll")
    return 0


def aggregate_listings(listings: list[dict[str, Any]]) -> dict[int, tuple[int, int, int, int, int, int]]:
    """
    Aggregates raw Blizzard AH listings into per-item stats:
    item_id -> (min_buyout, p10_price, median_price, mean_price, total_qty, listing_count)
    Uses unit_price (commodities) or buyout / quantity (item auctions).
    Skips bid-only listings (price <= 0).
    """
    by_item: dict[int, list[tuple[int, int]]] = defaultdict(list)

    for row in listings:
        item_obj = row.get("item")
        if not isinstance(item_obj, dict) or "id" not in item_obj:
            continue
        item_id = int(item_obj["id"])
        qty = int(row.get("quantity") or 1)
        if qty <= 0:
            qty = 1

        if "unit_price" in row and row["unit_price"]:
            unit_price = int(row["unit_price"])
        elif "buyout" in row and row["buyout"]:
            unit_price = int(row["buyout"]) // qty
        else:
            continue  # bid-only listing

        if unit_price <= 0:
            continue
        by_item[item_id].append((unit_price, qty))

    result: dict[int, tuple[int, int, int, int, int, int]] = {}
    for item_id, entries in by_item.items():
        entries.sort(key=lambda x: x[0])
        total_qty = sum(q for _, q in entries)
        listing_count = len(entries)
        min_buyout = entries[0][0]
        mean_price = int(sum(p * q for p, q in entries) / total_qty)

        # Quantity-weighted quantiles (p10 and p50/median)
        p10_target = max(1, math.ceil(total_qty * 0.10))
        p50_target = max(1, math.ceil(total_qty * 0.50))
        p10_price = min_buyout
        median_price = min_buyout
        cum = 0
        got_p10 = False
        for p, q in entries:
            cum += q
            if not got_p10 and cum >= p10_target:
                p10_price = p
                got_p10 = True
            if cum >= p50_target:
                median_price = p
                break

        result[item_id] = (min_buyout, p10_price, median_price, mean_price, total_qty, listing_count)

    return result


def poll_endpoint(
    client: httpx.Client,
    conn: sqlite3.Connection,
    headers: dict[str, str],
    region: str,
    ruleset_id: int,
    house_id: int,
    endpoint_type: str,
    force: bool = False,
) -> dict[str, Any]:
    ns = api_namespace(region)
    base = api_base(region)
    if endpoint_type == "auctions":
        url = f"{base}/data/wow/ruleset/{ruleset_id}/auctions/{house_id}"
    else:
        url = f"{base}/data/wow/ruleset/{ruleset_id}/auctions/{house_id}/commodities"

    req_headers = dict(headers)
    if not force:
        row = conn.execute(
            """
            SELECT last_modified FROM ah_snapshots
            WHERE region = ? AND ruleset_id = ? AND house_id = ? AND endpoint_type = ?
            ORDER BY snapshot_id DESC LIMIT 1
            """,
            (region, ruleset_id, house_id, endpoint_type),
        ).fetchone()
        if row and row["last_modified"]:
            req_headers["If-Modified-Since"] = row["last_modified"]

    resp = client.get(url, headers=req_headers, params={"namespace": ns}, timeout=60.0)
    if resp.status_code == 304:
        return {"endpoint": endpoint_type, "status": "not_modified"}
    if resp.status_code == 404:
        return {"endpoint": endpoint_type, "status": "not_found", "url": url}
    if resp.status_code != 200:
        raise RuntimeError(f"GET {url} failed ({resp.status_code}): {resp.text[:300]}")

    last_mod = resp.headers.get("Last-Modified") or utc_now_iso()
    data = resp.json()
    listings = data.get("auctions") or []
    agg = aggregate_listings(listings)
    now = utc_now_iso()

    with conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO ah_snapshots
                (region, ruleset_id, house_id, endpoint_type, last_modified, fetched_at, raw_count, unique_items)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (region, ruleset_id, house_id, endpoint_type, last_mod, now, len(listings), len(agg)),
        )
        if cur.rowcount == 0:
            return {"endpoint": endpoint_type, "status": "duplicate_skipped", "last_modified": last_mod}

        snap_id = cur.lastrowid
        conn.executemany(
            """
            INSERT INTO ah_item_prices
                (snapshot_id, item_id, min_buyout, p10_price, median_price, mean_price, total_qty, listing_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (snap_id, item_id, mn, p10, med, avg, qty, cnt)
                for item_id, (mn, p10, med, avg, qty, cnt) in agg.items()
            ],
        )

    return {
        "endpoint": endpoint_type,
        "status": "ingested",
        "snapshot_id": snap_id,
        "last_modified": last_mod,
        "raw_listings": len(listings),
        "unique_items": len(agg),
    }


def cmd_poll(args: argparse.Namespace) -> int:
    cfg = load_config()
    client_id, client_secret = require_credentials(cfg)
    region = args.region or cfg["region"]
    ruleset_id_str = str(args.ruleset or cfg["ruleset_id"])
    house_id_str = str(args.house or cfg["house_id"])

    if not ruleset_id_str or not house_id_str:
        print(
            "ERROR: Missing --ruleset / --house (or FOREVER_RULESET_ID / FOREVER_HOUSE_ID in telemetry/.env).\n"
            "Run `python telemetry/ah_poller.py discover` first.",
            file=sys.stderr,
        )
        return 2

    ruleset_id = int(ruleset_id_str)
    house_id = int(house_id_str)
    db_path = Path(args.db)
    init_db(db_path)

    conn = open_db(db_path)
    try:
        with httpx.Client() as client:
            token = get_oauth_token(client, client_id, client_secret)
            headers = {
                "Authorization": f"Bearer {token}",
                "Battlenet-Namespace": api_namespace(region),
            }
            results = []
            for ep in ("auctions", "commodities"):
                results.append(
                    poll_endpoint(
                        client, conn, headers, region, ruleset_id, house_id, ep, force=args.force
                    )
                )
    finally:
        conn.close()

    print(json.dumps({"ok": True, "region": region, "ruleset_id": ruleset_id, "house_id": house_id, "results": results}, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database does not exist yet: {db_path} (run `init`)")
        return 0
    conn = open_db(db_path)
    try:
        houses = [dict(r) for r in conn.execute("SELECT * FROM ah_houses ORDER BY region, ruleset_id, house_id")]
        snaps = [
            dict(r)
            for r in conn.execute(
                """
                SELECT region, ruleset_id, house_id, endpoint_type,
                       COUNT(*) AS snapshot_count,
                       MAX(last_modified) AS latest_modified,
                       MAX(fetched_at) AS latest_fetched,
                       SUM(unique_items) AS total_item_rows
                FROM ah_snapshots
                GROUP BY region, ruleset_id, house_id, endpoint_type
                """
            )
        ]
    finally:
        conn.close()

    print(json.dumps({"db_path": str(db_path), "houses": houses, "snapshots": snaps}, indent=2))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 1
    conn = open_db(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM ah_latest_prices WHERE item_id = ? ORDER BY endpoint_type",
            (args.item,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print(f"No live listings found for item_id={args.item}")
        return 0
    for r in rows:
        print(
            f"[{r['endpoint_type']}] item={r['item_id']}  "
            f"min={format_copper(r['min_buyout'])}  p10={format_copper(r['p10_price'])}  "
            f"med={format_copper(r['median_price'])}  qty={r['total_qty']}  "
            f"listings={r['listing_count']}  updated={r['last_modified']}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="WoW Forever Live AH Telemetry Poller")
    p.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to SQLite DB")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Initialize SQLite database and schema")

    p_disc = sub.add_parser("discover", help="Discover Forever rulesets & faction auction houses")
    p_disc.add_argument("--region", default="", help="Region override (default: eu)")

    p_poll = sub.add_parser("poll", help="Poll live auctions & commodities (idempotent)")
    p_poll.add_argument("--region", default="", help="Region override (default: eu)")
    p_poll.add_argument("--ruleset", type=int, default=0, help="Ruleset ID")
    p_poll.add_argument("--house", type=int, default=0, help="Auction House ID")
    p_poll.add_argument("--force", action="store_true", help="Ignore If-Modified-Since cache")

    sub.add_parser("status", help="Show discovered houses and snapshot counts")

    p_q = sub.add_parser("query", help="Look up latest price for an item ID")
    p_q.add_argument("--item", type=int, required=True, help="WoW Item ID")
    return p


def main() -> int:
    args = build_parser().parse_args()
    handlers = {
        "init": cmd_init,
        "discover": cmd_discover,
        "poll": cmd_poll,
        "status": cmd_status,
        "query": cmd_query,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
