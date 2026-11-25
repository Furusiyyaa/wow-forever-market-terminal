#!/usr/bin/env python3
"""
WoW Forever Telemetry Local API Server (for Electron TradingView App)
---------------------------------------------------------------------
Serves live AH prices, historical price/volume series, OHLC candles,
watchlists, market movers, and open-world farm rankings from the shared
SQLite WAL database (`work/telemetry/wow_forever.db`), while managing the
background auto-rescan poller and writing `work/telemetry/poller_status.json`
for the `forever-telemetry` MCP bridge.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "telemetry"))

import ah_poller  # noqa: E402
import db_ingest  # noqa: E402
import farm_ranker  # noqa: E402

DB_PATH = REPO_ROOT / "work" / "telemetry" / "wow_forever.db"
STATE_PATH = REPO_ROOT / "work" / "telemetry" / "gui_state.json"
STATUS_PATH = REPO_ROOT / "work" / "telemetry" / "poller_status.json"
WATCHLIST_PATH = REPO_ROOT / "work" / "telemetry" / "watchlist.json"

DEFAULT_WATCHLIST = [
    12363,   # Arcane Crystal
    248819,  # Fel Crystal
    251290,  # Legionite Bar
    248824,  # Fel Hide
    13468,   # Black Lotus
    248822,  # Death Lotus
    12811,   # Righteous Orb
    7082,    # Essence of Air
    7080,    # Essence of Water
    7076,    # Essence of Earth
    14227,   # Ironweb Spider Silk
    14047,   # Runecloth
    16203,   # Greater Eternal Essence
    14344,   # Large Brilliant Shard
]


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_ts(iso_str: str) -> int:
    try:
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return int(time.time())


def open_ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON;")
    return conn


def load_json(path: Path, default: Any) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


class PollerController:
    def __init__(self) -> None:
        saved = load_json(STATE_PATH, {"auto_rescan": True, "interval_minutes": 15})
        self.cfg = ah_poller.load_config()
        self.online: bool = False
        self.scanning: bool = False
        self.auto_rescan: bool = bool(saved.get("auto_rescan", True))
        self.interval_minutes: int = max(1, min(240, int(saved.get("interval_minutes", 15))))
        self.last_scan_utc: str | None = None
        self.next_scan_at: float | None = None
        self.last_result: str = "Idle"
        self.last_error: str | None = None
        self.logs: list[dict[str, str]] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._load_last_scan_from_db()
        self.add_log("Telemetry engine ready.")
        self.publish_heartbeat()
        self._thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._thread.start()

    def _load_last_scan_from_db(self) -> None:
        if not DB_PATH.exists():
            return
        try:
            conn = open_ro()
            row = conn.execute("SELECT MAX(fetched_at) AS ts FROM ah_snapshots").fetchone()
            conn.close()
            if row and row["ts"]:
                self.last_scan_utc = row["ts"]
        except Exception:
            pass

    def add_log(self, msg: str, level: str = "info") -> None:
        entry = {"ts": utc_iso(), "level": level, "msg": msg}
        self.logs.append(entry)
        if len(self.logs) > 100:
            self.logs = self.logs[-100:]

    def publish_heartbeat(self) -> None:
        next_iso = None
        if self.online and self.next_scan_at:
            next_iso = datetime.fromtimestamp(self.next_scan_at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        save_json(
            STATUS_PATH,
            {
                "pid": os.getpid(),
                "online": self.online,
                "scanning": self.scanning,
                "auto_rescan": self.auto_rescan,
                "interval_minutes": self.interval_minutes,
                "region": self.cfg["region"],
                "ruleset_id": self.cfg["ruleset_id"],
                "house_id": self.cfg["house_id"],
                "last_scan_utc": self.last_scan_utc,
                "next_scan_utc": next_iso,
                "last_result": self.last_result,
                "last_error": self.last_error,
                "heartbeat_utc": utc_iso(),
            },
        )

    def update_settings(
        self,
        online: bool | None = None,
        auto_rescan: bool | None = None,
        interval_minutes: int | None = None,
    ) -> None:
        trigger_initial = False
        with self._lock:
            if auto_rescan is not None:
                self.auto_rescan = bool(auto_rescan)
            if interval_minutes is not None:
                self.interval_minutes = max(1, min(240, int(interval_minutes)))
            save_json(
                STATE_PATH,
                {"auto_rescan": self.auto_rescan, "interval_minutes": self.interval_minutes},
            )
            if online is not None and online != self.online:
                self.online = bool(online)
                if self.online:
                    self.add_log("Telemetry service started (ONLINE).", "ok")
                    if self.auto_rescan:
                        self.next_scan_at = time.time() + self.interval_minutes * 60
                    trigger_initial = True
                else:
                    self.next_scan_at = None
                    self.add_log("Telemetry service stopped (OFFLINE).", "warn")
            elif self.online:
                if self.auto_rescan:
                    self.next_scan_at = time.time() + self.interval_minutes * 60
                else:
                    self.next_scan_at = None
            self.publish_heartbeat()

        if trigger_initial:
            self.trigger_poll_async(force=False)

    def trigger_poll_async(self, force: bool = False) -> bool:
        with self._lock:
            if self.scanning:
                return False
            self.scanning = True
            self.publish_heartbeat()
        threading.Thread(target=self._do_poll, args=(force,), daemon=True).start()
        return True

    def _do_poll(self, force: bool) -> None:
        try:
            cfg = ah_poller.load_config()
            cid, csec = ah_poller.require_credentials(cfg)
            region = cfg["region"]
            rid = int(cfg["ruleset_id"])
            hid = int(cfg["house_id"])
            ah_poller.init_db(DB_PATH)
            conn = ah_poller.open_db(DB_PATH)
            try:
                with httpx.Client() as client:
                    tok = ah_poller.get_oauth_token(client, cid, csec)
                    headers = {
                        "Authorization": f"Bearer {tok}",
                        "Battlenet-Namespace": ah_poller.api_namespace(region),
                    }
                    res_a = ah_poller.poll_endpoint(client, conn, headers, region, rid, hid, "auctions", force=force)
                    res_c = ah_poller.poll_endpoint(client, conn, headers, region, rid, hid, "commodities", force=force)
            finally:
                conn.close()

            self.last_scan_utc = utc_iso()
            self.last_error = None
            summary = f"auctions={res_a['status']}, commodities={res_c['status']}"
            if res_a["status"] == "ingested" or res_c["status"] == "ingested":
                raw_n = res_a.get("raw_listings", 0) + res_c.get("raw_listings", 0)
                uniq_n = res_a.get("unique_items", 0) + res_c.get("unique_items", 0)
                summary += f" (+{raw_n:,} listings / {uniq_n:,} items)"
            self.last_result = summary
            self.add_log(f"AH scan complete: {summary}", "ok")
        except Exception as exc:
            self.last_error = str(exc)
            self.last_result = f"ERROR: {exc}"
            self.add_log(f"AH scan failed: {exc}", "err")
        finally:
            with self._lock:
                self.scanning = False
                if self.online and self.auto_rescan:
                    self.next_scan_at = time.time() + self.interval_minutes * 60
                self.publish_heartbeat()

    def trigger_db2_sync_async(self) -> bool:
        with self._lock:
            if self.scanning:
                return False
            self.scanning = True
            self.publish_heartbeat()
        threading.Thread(target=self._do_db2_sync, daemon=True).start()
        return True

    def _do_db2_sync(self) -> None:
        try:
            self.add_log("Syncing latest WoW Forever DB2 from wago.tools...", "info")
            res = db_ingest.sync_db2(DB_PATH)
            self.add_log(
                f"DB2 synced: build {res['build']} ({res['items']:,} items, {res['areas']:,} areas)",
                "ok",
            )
        except Exception as exc:
            self.add_log(f"DB2 sync failed: {exc}", "err")
        finally:
            with self._lock:
                self.scanning = False
                self.publish_heartbeat()

    def _scheduler_loop(self) -> None:
        ticks = 0
        while not self._stop_event.is_set():
            time.sleep(1.0)
            ticks += 1
            if self.online and self.auto_rescan and not self.scanning and self.next_scan_at:
                if time.time() >= self.next_scan_at:
                    self.trigger_poll_async(force=False)
            if self.online and ticks % 15 == 0:
                self.publish_heartbeat()

    def shutdown(self) -> None:
        self._stop_event.set()
        self.online = False
        self.next_scan_at = None
        self.publish_heartbeat()


ctrl = PollerController()


async def ep_status(_req: Request) -> JSONResponse:
    db_info: dict[str, Any] = {"exists": DB_PATH.exists()}
    if DB_PATH.exists():
        conn = open_ro()
        try:
            db_info["latest_snapshots"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT endpoint_type, last_modified, fetched_at, raw_count, unique_items FROM ah_latest_snapshots"
                )
            ]
            db_info["total_snapshots"] = conn.execute("SELECT COUNT(*) FROM ah_snapshots").fetchone()[0]
            db_info["items_count"] = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            db_info["meta"] = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM db_meta")}
        finally:
            conn.close()

    next_sec = None
    if ctrl.online and ctrl.auto_rescan and ctrl.next_scan_at:
        next_sec = max(0, int(ctrl.next_scan_at - time.time()))

    return JSONResponse(
        {
            "online": ctrl.online,
            "scanning": ctrl.scanning,
            "auto_rescan": ctrl.auto_rescan,
            "interval_minutes": ctrl.interval_minutes,
            "region": ctrl.cfg["region"],
            "ruleset_id": ctrl.cfg["ruleset_id"],
            "house_id": ctrl.cfg["house_id"],
            "last_scan_utc": ctrl.last_scan_utc,
            "next_scan_in_sec": next_sec,
            "last_result": ctrl.last_result,
            "last_error": ctrl.last_error,
            "logs": ctrl.logs[-40:],
            "database": db_info,
        }
    )


async def ep_service(req: Request) -> JSONResponse:
    body = await req.json()
    ctrl.update_settings(
        online=body.get("online"),
        auto_rescan=body.get("auto_rescan"),
        interval_minutes=body.get("interval_minutes"),
    )
    return await ep_status(req)


async def ep_poll(req: Request) -> JSONResponse:
    force = False
    try:
        body = await req.json()
        force = bool(body.get("force", False))
    except Exception:
        pass
    started = ctrl.trigger_poll_async(force=force)
    return JSONResponse({"started": started})


async def ep_sync_db2(_req: Request) -> JSONResponse:
    started = ctrl.trigger_db2_sync_async()
    return JSONResponse({"started": started})


async def ep_search(req: Request) -> JSONResponse:
    q = (req.query_params.get("q") or "").strip()
    limit = max(1, min(50, int(req.query_params.get("limit") or 25)))
    conn = open_ro()
    try:
        if not q:
            rows = conn.execute(
                """
                SELECT item_id, name, quality, ah_p10_price, ah_min_buyout, ah_total_qty, sell_price
                FROM v_item_value
                WHERE ah_total_qty >= 10
                ORDER BY (ah_p10_price * ah_total_qty) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        elif q.isdigit():
            rows = conn.execute(
                """
                SELECT item_id, name, quality, ah_p10_price, ah_min_buyout, ah_total_qty, sell_price
                FROM v_item_value
                WHERE item_id = ?
                LIMIT ?
                """,
                (int(q), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT item_id, name, quality, ah_p10_price, ah_min_buyout, ah_total_qty, sell_price
                FROM v_item_value
                WHERE name LIKE ?
                ORDER BY
                    CASE WHEN LOWER(name) = LOWER(?) THEN 0
                         WHEN LOWER(name) LIKE LOWER(?) THEN 1
                         ELSE 2 END,
                    ah_total_qty DESC, ah_p10_price DESC
                LIMIT ?
                """,
                (f"%{q}%", q, f"{q}%", limit),
            ).fetchall()
    finally:
        conn.close()

    return JSONResponse(
        {
            "items": [
                {
                    "item_id": r["item_id"],
                    "name": r["name"],
                    "quality": r["quality"],
                    "p10_g": round((r["ah_p10_price"] or 0) / 10000.0, 4),
                    "min_g": round((r["ah_min_buyout"] or 0) / 10000.0, 4),
                    "sell_g": round((r["sell_price"] or 0) / 10000.0, 4),
                    "qty": r["ah_total_qty"] or 0,
                }
                for r in rows
            ]
        }
    )


def get_watchlist_ids() -> list[int]:
    ids = load_json(WATCHLIST_PATH, DEFAULT_WATCHLIST)
    return [int(x) for x in ids if isinstance(x, int) or str(x).isdigit()]


async def ep_watchlist_get(_req: Request) -> JSONResponse:
    ids = get_watchlist_ids()
    if not ids:
        return JSONResponse({"items": []})
    conn = open_ro()
    try:
        ph = ",".join("?" for _ in ids)
        latest_map = {
            r["item_id"]: dict(r)
            for r in conn.execute(
                f"SELECT item_id, name, quality, ah_p10_price, ah_min_buyout, ah_total_qty, sell_price FROM v_item_value WHERE item_id IN ({ph})",
                ids,
            )
        }
        # Previous snapshot comparison for % change
        hist_rows = conn.execute(
            f"""
            SELECT p.item_id, p.p10_price, s.fetched_at
            FROM ah_item_prices p
            JOIN ah_snapshots s ON s.snapshot_id = p.snapshot_id
            WHERE p.item_id IN ({ph})
            ORDER BY s.fetched_at ASC
            """,
            ids,
        ).fetchall()
    finally:
        conn.close()

    by_item: dict[int, list[int]] = defaultdict(list)
    for r in hist_rows:
        by_item[int(r["item_id"])].append(int(r["p10_price"]))

    out = []
    for iid in ids:
        row = latest_map.get(iid)
        if not row:
            continue
        series = by_item.get(iid, [])
        p10 = int(row["ah_p10_price"] or 0)
        prev = series[-2] if len(series) >= 2 else (series[0] if series else p10)
        pct = round(((p10 - prev) / prev) * 100.0, 2) if prev > 0 else 0.0
        out.append(
            {
                "item_id": iid,
                "name": row["name"],
                "quality": row["quality"],
                "p10_g": round(p10 / 10000.0, 4),
                "min_g": round((row["ah_min_buyout"] or 0) / 10000.0, 4),
                "qty": row["ah_total_qty"] or 0,
                "change_pct": pct,
                "points": len(series),
            }
        )
    return JSONResponse({"items": out})


async def ep_watchlist_post(req: Request) -> JSONResponse:
    body = await req.json()
    action = body.get("action")
    iid = int(body.get("item_id"))
    ids = get_watchlist_ids()
    if action == "add" and iid not in ids:
        ids.insert(0, iid)
    elif action == "remove" and iid in ids:
        ids.remove(iid)
    save_json(WATCHLIST_PATH, ids)
    return await ep_watchlist_get(req)


async def ep_item_history(req: Request) -> JSONResponse:
    iid = int(req.path_params["item_id"])
    rng = (req.query_params.get("range") or "7d").lower()
    cutoff_sql = {
        "24h": "datetime('now', '-1 day')",
        "7d": "datetime('now', '-7 days')",
        "30d": "datetime('now', '-30 days')",
        "all": "'1970-01-01T00:00:00Z'",
    }.get(rng, "datetime('now', '-7 days')")

    conn = open_ro()
    try:
        meta = conn.execute(
            """
            SELECT v.*, i.description, i.item_level, i.required_level, i.stackable
            FROM v_item_value v
            JOIN items i ON i.item_id = v.item_id
            WHERE v.item_id = ?
            """,
            (iid,),
        ).fetchone()
        if not meta:
            return JSONResponse({"error": "Item not found"}, status_code=404)

        rows = conn.execute(
            f"""
            SELECT s.fetched_at, s.last_modified, s.endpoint_type,
                   p.min_buyout, p.p10_price, p.median_price, p.mean_price,
                   p.total_qty, p.listing_count
            FROM ah_item_prices p
            JOIN ah_snapshots s ON s.snapshot_id = p.snapshot_id
            WHERE p.item_id = ? AND s.fetched_at >= {cutoff_sql}
            ORDER BY s.fetched_at ASC
            """,
            (iid,),
        ).fetchall()
    finally:
        conn.close()

    # Deduplicate timestamps (lightweight-charts requires strictly ascending Unix timestamps)
    points = []
    seen_ts = -1
    for r in rows:
        ts = parse_iso_ts(r["fetched_at"])
        if ts <= seen_ts:
            ts = seen_ts + 1
        seen_ts = ts
        points.append(
            {
                "time": ts,
                "fetched_at": r["fetched_at"],
                "last_modified": r["last_modified"],
                "min_g": round(r["min_buyout"] / 10000.0, 4),
                "p10_g": round(r["p10_price"] / 10000.0, 4),
                "median_g": round(r["median_price"] / 10000.0, 4),
                "mean_g": round(r["mean_price"] / 10000.0, 4),
                "qty": int(r["total_qty"]),
                "listings": int(r["listing_count"]),
            }
        )

    # Build OHLC candles (grouped into 4-hour buckets for 24h/7d, daily for 30d/all)
    bucket_sec = 86400 if rng in ("30d", "all") else 14400
    buckets: dict[int, list[dict]] = defaultdict(list)
    for pt in points:
        b = (pt["time"] // bucket_sec) * bucket_sec
        buckets[b].append(pt)

    candles = []
    for b_ts in sorted(buckets.keys()):
        grp = buckets[b_ts]
        candles.append(
            {
                "time": b_ts,
                "open": grp[0]["p10_g"],
                "high": max(max(x["p10_g"], x["median_g"]) for x in grp),
                "low": min(min(x["min_g"], x["p10_g"]) for x in grp),
                "close": grp[-1]["p10_g"],
                "volume": int(sum(x["qty"] for x in grp) / len(grp)),
            }
        )

    first_p = points[0]["p10_g"] if points else 0.0
    last_p = points[-1]["p10_g"] if points else round((meta["ah_p10_price"] or 0) / 10000.0, 4)
    pct = round(((last_p - first_p) / first_p) * 100.0, 2) if first_p > 0 else 0.0

    return JSONResponse(
        {
            "item": {
                "item_id": meta["item_id"],
                "name": meta["name"],
                "quality": meta["quality"],
                "item_level": meta["item_level"],
                "required_level": meta["required_level"],
                "sell_g": round((meta["sell_price"] or 0) / 10000.0, 4),
                "p10_g": round((meta["ah_p10_price"] or 0) / 10000.0, 4),
                "min_g": round((meta["ah_min_buyout"] or 0) / 10000.0, 4),
                "qty": meta["ah_total_qty"] or 0,
                "listings": meta["ah_listing_count"] or 0,
                "change_pct": pct,
                "description": meta["description"] or "",
            },
            "points": points,
            "candles": candles,
        }
    )


async def ep_farms(req: Request) -> JSONResponse:
    min_level = int(req.query_params.get("min_level") or 45)
    limit = max(1, min(50, int(req.query_params.get("limit") or 25)))
    conn = open_ro()
    try:
        items = farm_ranker.load_item_values(conn)
        ref_evs = farm_ranker.compute_reference_evs(conn, items)
        loot_evs = farm_ranker.compute_loot_evs(conn, items, ref_evs)
        rows = []
        for c in conn.execute(
            """
            SELECT entry, name, min_level, max_level, creature_type,
                   min_loot_gold, max_loot_gold, loot_id, spawn_count, primary_map
            FROM creatures
            WHERE rank = 0 AND max_level BETWEEN ? AND 60
              AND primary_map IN (0, 1) AND spawn_count >= 15
            """,
            (min_level,),
        ):
            coin = (int(c["min_loot_gold"]) + int(c["max_loot_gold"])) / 2.0
            le = loot_evs.get(int(c["loot_id"]), {"direct": 0.0, "ref": 0.0, "drops": []})
            total_g = (coin + le["direct"] + le["ref"]) / 10000.0
            top = sorted(le["drops"], key=lambda x: x[0], reverse=True)[:2]
            rows.append(
                {
                    "name": c["name"],
                    "level": f"{c['min_level']}-{c['max_level']}",
                    "type": farm_ranker.CREATURE_TYPES.get(int(c["creature_type"]), "Other"),
                    "cont": "EK" if int(c["primary_map"]) == 0 else "Kal",
                    "spawns": int(c["spawn_count"]),
                    "ev_g": round(total_g, 3),
                    "drops": ", ".join(f"{nm} ({ch:.0f}%)" for _, nm, ch, _, _ in top),
                }
            )
    finally:
        conn.close()
    rows.sort(key=lambda r: r["ev_g"], reverse=True)
    return JSONResponse({"farms": rows[:limit]})


routes = [
    Route("/api/status", ep_status, methods=["GET"]),
    Route("/api/service", ep_service, methods=["POST"]),
    Route("/api/poll", ep_poll, methods=["POST"]),
    Route("/api/sync-db2", ep_sync_db2, methods=["POST"]),
    Route("/api/search", ep_search, methods=["GET"]),
    Route("/api/watchlist", ep_watchlist_get, methods=["GET"]),
    Route("/api/watchlist", ep_watchlist_post, methods=["POST"]),
    Route("/api/item/{item_id:int}/history", ep_item_history, methods=["GET"]),
    Route("/api/farms", ep_farms, methods=["GET"]),
]

@contextlib.asynccontextmanager
async def lifespan(_app: Starlette):
    yield
    ctrl.shutdown()


app = Starlette(routes=routes, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


if __name__ == "__main__":
    port = int(os.getenv("TELEMETRY_API_PORT", "8765"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
