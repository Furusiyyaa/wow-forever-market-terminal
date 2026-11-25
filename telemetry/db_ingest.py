#!/usr/bin/env python3
"""
WoW Forever Static Catalog & Classic World Loot Ingestion (Stage 2)
-------------------------------------------------------------------
1. `sync-db2`: Auto-detects the latest `wowf` (WoW Forever) build from wago.tools
   and ingests ItemSparse, Item, ItemClass, ItemSubClass, and AreaTable CSVs into
   work/telemetry/wow_forever.db.
2. `sync-classic-db`: Downloads CMaNGOS ClassicDB (1.12.1 world dump) to local
   cache and ingests creature_template, creature_loot_template,
   reference_loot_template, and creature spawn counts into the same SQLite DB.
3. `sync-all`: Runs both in sequence.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "telemetry" / "schema.sql"
DEFAULT_DB_PATH = REPO_ROOT / "work" / "telemetry" / "wow_forever.db"
CACHE_DIR = REPO_ROOT / "work" / "telemetry" / "cache"

WAGO_BUILDS_URL = "https://wago.tools/api/builds"
WAGO_CSV_URL = "https://wago.tools/db2/{table}/csv?build={build}"
CLASSICDB_URL = (
    "https://raw.githubusercontent.com/cmangos/classic-db/master/Full_DB/"
    "ClassicDB_1_12_1_z2815.sql.gz"
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def to_int(val: str | None, default: int = 0) -> int:
    if val is None or val == "":
        return default
    try:
        return int(float(val))
    except ValueError:
        return default


def to_float(val: str | None, default: float = 0.0) -> float:
    if val is None or val == "":
        return default
    try:
        return float(val)
    except ValueError:
        return default


def get_latest_wowf_build(client: httpx.Client) -> str:
    r = client.get(WAGO_BUILDS_URL, timeout=30.0)
    r.raise_for_status()
    builds = r.json().get("wowf") or []
    if not builds:
        raise RuntimeError("No `wowf` builds found in wago.tools/api/builds")
    return str(builds[0]["version"])


def fetch_wago_csv(client: httpx.Client, table: str, build: str) -> list[dict[str, str]]:
    url = WAGO_CSV_URL.format(table=table, build=build)
    r = client.get(url, timeout=90.0)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.text)))


def sync_db2(db_path: Path, build_override: str = "") -> dict[str, object]:
    with httpx.Client(follow_redirects=True) as client:
        build = build_override or get_latest_wowf_build(client)
        print(f"[db2] Using WoW Forever build: {build}")

        item_classes_rows = fetch_wago_csv(client, "ItemClass", build)
        item_subclasses_rows = fetch_wago_csv(client, "ItemSubClass", build)
        area_rows = fetch_wago_csv(client, "AreaTable", build)
        item_rows = fetch_wago_csv(client, "Item", build)
        sparse_rows = fetch_wago_csv(client, "ItemSparse", build)

    item_meta: dict[int, tuple[int, int]] = {}
    for row in item_rows:
        iid = to_int(row.get("ID"), -1)
        if iid >= 0:
            item_meta[iid] = (to_int(row.get("ClassID"), -1), to_int(row.get("SubclassID"), -1))

    conn = open_db(db_path)
    try:
        with conn:
            conn.execute("DELETE FROM item_classes")
            conn.executemany(
                "INSERT OR REPLACE INTO item_classes (class_id, name) VALUES (?, ?)",
                [
                    (to_int(r.get("ClassID"), -1), (r.get("ClassName_lang") or "").strip())
                    for r in item_classes_rows
                    if r.get("ClassID") not in (None, "")
                ],
            )

            conn.execute("DELETE FROM item_subclasses")
            conn.executemany(
                "INSERT OR REPLACE INTO item_subclasses (class_id, subclass_id, name) VALUES (?, ?, ?)",
                [
                    (
                        to_int(r.get("ClassID"), -1),
                        to_int(r.get("SubClassID"), -1),
                        (r.get("DisplayName_lang") or r.get("VerboseName_lang") or "").strip(),
                    )
                    for r in item_subclasses_rows
                    if r.get("ClassID") not in (None, "") and r.get("SubClassID") not in (None, "")
                ],
            )

            conn.execute("DELETE FROM areas")
            conn.executemany(
                """
                INSERT OR REPLACE INTO areas
                    (area_id, continent_id, parent_area_id, name, exploration_level)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        to_int(r.get("ID"), 0),
                        to_int(r.get("ContinentID"), 0),
                        to_int(r.get("ParentAreaID"), 0),
                        (r.get("AreaName_lang") or r.get("ZoneName") or "").strip(),
                        to_int(r.get("ExplorationLevel"), 0),
                    )
                    for r in area_rows
                    if to_int(r.get("ID"), 0) > 0
                ],
            )

            conn.execute("DELETE FROM items")
            items_to_insert = []
            for r in sparse_rows:
                iid = to_int(r.get("ID"), 0)
                if iid <= 0:
                    continue
                cls_id, sub_id = item_meta.get(iid, (-1, -1))
                items_to_insert.append(
                    (
                        iid,
                        (r.get("Display_lang") or "").strip(),
                        to_int(r.get("OverallQualityID"), 0),
                        to_int(r.get("ItemLevel"), 0),
                        to_int(r.get("RequiredLevel"), 0),
                        cls_id,
                        sub_id,
                        to_int(r.get("SellPrice"), 0),
                        to_int(r.get("BuyPrice"), 0),
                        max(1, to_int(r.get("Stackable"), 1)),
                        to_int(r.get("Bonding"), 0),
                        to_int(r.get("InventoryType"), 0),
                        (r.get("Description_lang") or "").strip(),
                    )
                )
            conn.executemany(
                """
                INSERT OR REPLACE INTO items
                    (item_id, name, quality, item_level, required_level, class_id, subclass_id,
                     sell_price, buy_price, stackable, bonding, inventory_type, description)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                items_to_insert,
            )

            conn.execute(
                "INSERT OR REPLACE INTO db_meta (key, value, updated_at) VALUES (?, ?, ?)",
                ("wowf_db2_build", build, utc_now_iso()),
            )
    finally:
        conn.close()

    return {
        "build": build,
        "items": len(items_to_insert),
        "item_classes": len(item_classes_rows),
        "item_subclasses": len(item_subclasses_rows),
        "areas": len(area_rows),
    }


def parse_mysql_tuples(values_str: str) -> Iterable[list[str | None]]:
    """
    Fast streaming parser for MySQL `INSERT INTO ... VALUES (...),(...);` tuples.
    Handles single-quoted strings with backslash escapes and NULLs.
    """
    n = len(values_str)
    i = 0
    while i < n:
        # Advance to '('
        while i < n and values_str[i] != "(":
            i += 1
        if i >= n:
            break
        i += 1  # skip '('
        row: list[str | None] = []
        while i < n:
            while i < n and values_str[i] in " \t\r\n":
                i += 1
            if i >= n:
                break
            ch = values_str[i]
            if ch == "'":
                i += 1
                buf = []
                while i < n:
                    c = values_str[i]
                    if c == "\\" and i + 1 < n:
                        nxt = values_str[i + 1]
                        buf.append({"n": "\n", "r": "\r", "t": "\t", "'": "'", "\\": "\\"}.get(nxt, nxt))
                        i += 2
                    elif c == "'":
                        if i + 1 < n and values_str[i + 1] == "'":
                            buf.append("'")
                            i += 2
                        else:
                            i += 1
                            break
                    else:
                        buf.append(c)
                        i += 1
                row.append("".join(buf))
            else:
                start = i
                while i < n and values_str[i] not in ",)":
                    i += 1
                tok = values_str[start:i].strip()
                row.append(None if tok.upper() == "NULL" else tok)

            while i < n and values_str[i] in " \t\r\n":
                i += 1
            if i < n and values_str[i] == ",":
                i += 1
                continue
            if i < n and values_str[i] == ")":
                i += 1
                yield row
                break


def parse_insert_columns(stmt_prefix: str) -> list[str] | None:
    """Extract column list if `INSERT INTO tbl (col1, col2) VALUES` has explicit columns."""
    lpar = stmt_prefix.find("(")
    rpar = stmt_prefix.rfind(")")
    if lpar == -1 or rpar == -1 or rpar < lpar:
        return None
    return [c.strip().strip("`") for c in stmt_prefix[lpar + 1 : rpar].split(",")]


def parse_create_table_columns(lines_iter: Iterable[str]) -> list[str]:
    cols: list[str] = []
    for line in lines_iter:
        s = line.strip()
        if s.startswith(")"):
            break
        if s.startswith("`"):
            end = s.find("`", 1)
            if end != -1:
                cols.append(s[1:end])
    return cols


def sync_classic_db(db_path: Path, force_download: bool = False) -> dict[str, object]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    gz_path = CACHE_DIR / "ClassicDB_1_12_1_z2815.sql.gz"

    if force_download or not gz_path.exists():
        print(f"[classic-db] Downloading {CLASSICDB_URL} ...")
        with httpx.Client(follow_redirects=True, timeout=120.0) as client:
            with client.stream("GET", CLASSICDB_URL) as r:
                r.raise_for_status()
                with gz_path.open("wb") as f:
                    for chunk in r.iter_bytes(chunk_size=65536):
                        f.write(chunk)
        print(f"[classic-db] Saved {gz_path.stat().st_size:,} bytes to {gz_path}")
    else:
        print(f"[classic-db] Using cached {gz_path}")

    target_tables = {
        "creature_template",
        "creature_loot_template",
        "reference_loot_template",
        "creature",
    }
    table_cols: dict[str, list[str]] = {}
    creatures_raw: dict[int, tuple] = {}
    creature_loot_rows: list[tuple] = []
    ref_loot_rows: list[tuple] = []
    spawn_maps: dict[int, Counter[int]] = defaultdict(Counter)

    with gzip.open(gz_path, "rt", encoding="utf-8", errors="replace") as f:
        it = iter(f)
        for line in it:
            if line.startswith("CREATE TABLE `"):
                tname = line.split("`", 2)[1]
                if tname in target_tables:
                    table_cols[tname] = parse_create_table_columns(it)
            elif line.startswith("INSERT INTO `"):
                tname = line.split("`", 2)[1]
                if tname not in target_tables:
                    continue
                val_idx = line.find("VALUES ")
                if val_idx == -1:
                    continue
                cols = parse_insert_columns(line[:val_idx]) or table_cols.get(tname)
                if not cols:
                    continue
                col_idx = {c.lower(): idx for idx, c in enumerate(cols)}
                values_part = line[val_idx + 7 :]

                if tname == "creature_template":
                    for row in parse_mysql_tuples(values_part):
                        entry = to_int(row[col_idx["entry"]], 0)
                        if entry <= 0:
                            continue
                        creatures_raw[entry] = (
                            entry,
                            row[col_idx["name"]] or "",
                            row[col_idx.get("subname", -1)] or "" if "subname" in col_idx else "",
                            to_int(row[col_idx["minlevel"]], 0),
                            to_int(row[col_idx["maxlevel"]], 0),
                            to_int(row[col_idx["rank"]], 0),
                            to_int(row[col_idx["creaturetype"]], 0),
                            to_int(row[col_idx.get("faction", col_idx.get("factionalliance", 0))], 0),
                            to_int(row[col_idx["minlootgold"]], 0),
                            to_int(row[col_idx["maxlootgold"]], 0),
                            to_int(row[col_idx["lootid"]], 0),
                            to_int(row[col_idx["skinninglootid"]], 0),
                        )
                elif tname == "creature":
                    id_i = col_idx["id"]
                    map_i = col_idx["map"]
                    for row in parse_mysql_tuples(values_part):
                        cid = to_int(row[id_i], 0)
                        mid = to_int(row[map_i], -1)
                        if cid > 0:
                            spawn_maps[cid][mid] += 1
                elif tname == "creature_loot_template":
                    e_i = col_idx["entry"]
                    it_i = col_idx["item"]
                    ch_i = col_idx["chanceorquestchance"]
                    g_i = col_idx["groupid"]
                    mn_i = col_idx["mincountorref"]
                    mx_i = col_idx["maxcount"]
                    for row in parse_mysql_tuples(values_part):
                        chance = to_float(row[ch_i], 0.0)
                        if chance < 0:
                            continue  # negative = quest-only drop
                        min_or_ref = to_int(row[mn_i], 1)
                        if min_or_ref < 0:
                            # Reference loot table entry
                            creature_loot_rows.append(
                                (to_int(row[e_i], 0), -min_or_ref, chance, to_int(row[g_i], 0), 1, max(1, to_int(row[mx_i], 1)), 1)
                            )
                        else:
                            creature_loot_rows.append(
                                (to_int(row[e_i], 0), to_int(row[it_i], 0), chance, to_int(row[g_i], 0), max(1, min_or_ref), max(1, to_int(row[mx_i], 1)), 0)
                            )
                elif tname == "reference_loot_template":
                    e_i = col_idx["entry"]
                    it_i = col_idx["item"]
                    ch_i = col_idx["chanceorquestchance"]
                    g_i = col_idx["groupid"]
                    mn_i = col_idx["mincountorref"]
                    mx_i = col_idx["maxcount"]
                    for row in parse_mysql_tuples(values_part):
                        chance = to_float(row[ch_i], 0.0)
                        min_or_ref = to_int(row[mn_i], 1)
                        if chance < 0 or min_or_ref < 0:
                            continue
                        ref_loot_rows.append(
                            (to_int(row[e_i], 0), to_int(row[it_i], 0), chance, to_int(row[g_i], 0), max(1, min_or_ref), max(1, to_int(row[mx_i], 1)))
                        )

    creatures_final = []
    for entry, tup in creatures_raw.items():
        maps = spawn_maps.get(entry)
        if maps:
            total_spawns = sum(maps.values())
            primary_map = maps.most_common(1)[0][0]
        else:
            total_spawns = 0
            primary_map = -1
        creatures_final.append((*tup, total_spawns, primary_map))

    conn = open_db(db_path)
    try:
        with conn:
            conn.execute("DELETE FROM creatures")
            conn.executemany(
                """
                INSERT OR REPLACE INTO creatures
                    (entry, name, subname, min_level, max_level, rank, creature_type,
                     faction, min_loot_gold, max_loot_gold, loot_id, skinning_loot_id,
                     spawn_count, primary_map)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                creatures_final,
            )

            conn.execute("DELETE FROM creature_loot")
            conn.executemany(
                """
                INSERT INTO creature_loot
                    (loot_id, item_id, chance, group_id, min_count, max_count, is_reference)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                creature_loot_rows,
            )

            conn.execute("DELETE FROM reference_loot")
            conn.executemany(
                """
                INSERT INTO reference_loot
                    (ref_id, item_id, chance, group_id, min_count, max_count)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ref_loot_rows,
            )

            conn.execute(
                "INSERT OR REPLACE INTO db_meta (key, value, updated_at) VALUES (?, ?, ?)",
                ("classic_db_source", "cmangos/classic-db:ClassicDB_1_12_1_z2815", utc_now_iso()),
            )
    finally:
        conn.close()

    return {
        "creatures": len(creatures_final),
        "creature_loot_rows": len(creature_loot_rows),
        "reference_loot_rows": len(ref_loot_rows),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="WoW Forever Static DB & Classic Loot Ingestion")
    p.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to SQLite DB")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_db2 = sub.add_parser("sync-db2", help="Sync Forever DB2 tables from wago.tools")
    p_db2.add_argument("--build", default="", help="Optional explicit build string")

    p_cdb = sub.add_parser("sync-classic-db", help="Sync CMaNGOS ClassicDB creatures & loot")
    p_cdb.add_argument("--force-download", action="store_true", help="Re-download ClassicDB archive")

    p_all = sub.add_parser("sync-all", help="Run both sync-db2 and sync-classic-db")
    p_all.add_argument("--build", default="", help="Optional explicit build string")
    p_all.add_argument("--force-download", action="store_true", help="Re-download ClassicDB archive")

    args = p.parse_args()
    db_path = Path(args.db)

    out: dict[str, object] = {"db_path": str(db_path)}
    if args.cmd in ("sync-db2", "sync-all"):
        out["db2"] = sync_db2(db_path, getattr(args, "build", ""))
    if args.cmd in ("sync-classic-db", "sync-all"):
        out["classic_db"] = sync_classic_db(db_path, getattr(args, "force_download", False))

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
