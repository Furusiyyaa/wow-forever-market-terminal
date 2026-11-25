#!/usr/bin/env python3
"""
WoW Forever Open-World Gold Farm Ranker (Stage 3)
-------------------------------------------------
Computes consistent Expected Value per Kill (EV/kill) across all open-world
non-elite mobs (Eastern Kingdoms & Kalimdor) using:
  - Raw coin drops
  - Direct creature_loot drops
  - Reference loot tables (proper CMaNGOS group_id / equal-chance math)
  - Live EU PvP Horde (house 113) AH p10 prices (95% after AH cut, qty >= 5)
    falling back to vendor sell_price for BoP/grays/illiquid items.

Separates "Consistent EV" from "<0.5% lottery jackpot" drops so rankings reflect
reliable gold per hour rather than 1-in-10,000 epic spikes.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "work" / "telemetry" / "wow_forever.db"

CREATURE_TYPES = {
    0: "None",
    1: "Beast",
    2: "Dragonkin",
    3: "Demon",
    4: "Elemental",
    5: "Giant",
    6: "Undead",
    7: "Humanoid",
    8: "Critter",
    9: "Mechanical",
    10: "Unk",
}

MAPS = {0: "Eastern Kingdoms", 1: "Kalimdor"}


def load_item_values(conn: sqlite3.Connection, min_ah_qty: int = 5) -> dict[int, dict]:
    items: dict[int, dict] = {}
    for r in conn.execute(
        """
        SELECT item_id, name, quality, class_id, bonding, sell_price,
               ah_p10_price, ah_total_qty, ah_listing_count
        FROM v_item_value
        """
    ):
        sell = int(r["sell_price"] or 0)
        p10 = int(r["ah_p10_price"] or 0)
        qty = int(r["ah_total_qty"] or 0)
        bonding = int(r["bonding"] or 0)
        # Liquid AH value (after 5% cut) if non-gray, non-BoP, non-Quest, and >= min_ah_qty listed
        quality = int(r["quality"] or 0)
        ah_net = int(p10 * 0.95) if (quality >= 1 and bonding not in (1, 4) and qty >= min_ah_qty and p10 > 0) else 0
        # Ignore manipulated white junk on AH (>50g with thin listings)
        if quality == 1 and ah_net > 500000 and int(r["ah_listing_count"] or 0) < 10:
            ah_net = 0
        eff = max(sell, ah_net)
        items[int(r["item_id"])] = {
            "name": r["name"],
            "quality": int(r["quality"] or 0),
            "class_id": int(r["class_id"] or -1),
            "sell": sell,
            "ah_net": ah_net,
            "ah_p10": p10,
            "ah_qty": qty,
            "eff": eff,
            "is_ah": ah_net > sell,
        }
    return items


def compute_reference_evs(
    conn: sqlite3.Connection, items: dict[int, dict]
) -> dict[int, tuple[float, float]]:
    """
    Returns ref_id -> (consistent_ev_copper, jackpot_ev_copper).
    Handles CMaNGOS group_id semantics:
      - group_id == 0: independent rolls at chance / 100
      - group_id > 0: mutually exclusive group; rows with chance == 0 split
        the remaining (100 - sum_explicit) probability equally.
    """
    by_ref_group: dict[tuple[int, int], list[tuple[int, float, int, int]]] = defaultdict(list)
    for r in conn.execute(
        "SELECT ref_id, item_id, chance, group_id, min_count, max_count FROM reference_loot"
    ):
        by_ref_group[(int(r["ref_id"]), int(r["group_id"]))].append(
            (int(r["item_id"]), float(r["chance"]), int(r["min_count"]), int(r["max_count"]))
        )

    ref_consistent: dict[int, float] = defaultdict(float)
    ref_jackpot: dict[int, float] = defaultdict(float)

    for (ref_id, group_id), rows in by_ref_group.items():
        if group_id == 0:
            for item_id, chance, mn, mx in rows:
                it = items.get(item_id)
                if not it or it["eff"] <= 0:
                    continue
                ev = (chance / 100.0) * ((mn + mx) / 2.0) * it["eff"]
                if chance < 0.5 and it["eff"] >= 200000:
                    ref_jackpot[ref_id] += ev
                else:
                    ref_consistent[ref_id] += ev
        else:
            explicit_sum = sum(ch for _, ch, _, _ in rows if ch > 0)
            zero_rows = [row for row in rows if row[1] == 0]
            rem = max(0.0, 100.0 - explicit_sum)
            eq_ch = (rem / len(zero_rows)) if zero_rows else 0.0
            for item_id, chance, mn, mx in rows:
                it = items.get(item_id)
                if not it or it["eff"] <= 0:
                    continue
                eff_ch = chance if chance > 0 else eq_ch
                ev = (eff_ch / 100.0) * ((mn + mx) / 2.0) * it["eff"]
                if eff_ch < 0.5 and it["eff"] >= 200000:
                    ref_jackpot[ref_id] += ev
                else:
                    ref_consistent[ref_id] += ev

    all_refs = set(ref_consistent.keys()) | set(ref_jackpot.keys())
    return {rid: (ref_consistent[rid], ref_jackpot[rid]) for rid in all_refs}


def compute_loot_evs(
    conn: sqlite3.Connection,
    items: dict[int, dict],
    ref_evs: dict[int, tuple[float, float]],
) -> dict[int, dict]:
    by_lid_group: dict[tuple[int, int], list[tuple[int, float, int, int, int]]] = defaultdict(list)
    for r in conn.execute(
        "SELECT loot_id, item_id, chance, group_id, min_count, max_count, is_reference FROM creature_loot"
    ):
        by_lid_group[(int(r["loot_id"]), int(r["group_id"]))].append(
            (
                int(r["item_id"]),
                float(r["chance"]),
                int(r["min_count"]),
                int(r["max_count"]),
                int(r["is_reference"]),
            )
        )

    out: dict[int, dict] = defaultdict(lambda: {"direct": 0.0, "ref": 0.0, "jackpot": 0.0, "drops": []})

    for (lid, gid), rows in by_lid_group.items():
        if gid > 0:
            explicit_sum = sum(ch for _, ch, _, _, _ in rows if ch > 0)
            zero_cnt = sum(1 for _, ch, _, _, _ in rows if ch == 0)
            eq_ch = (max(0.0, 100.0 - explicit_sum) / zero_cnt) if zero_cnt else 0.0
        else:
            eq_ch = 0.0

        for item_id, chance, mn, mx, is_ref in rows:
            eff_ch = chance if (gid == 0 or chance > 0) else eq_ch
            if eff_ch <= 0:
                continue
            avg_cnt = (mn + mx) / 2.0
            if is_ref:
                rc, rj = ref_evs.get(item_id, (0.0, 0.0))
                out[lid]["ref"] += (eff_ch / 100.0) * avg_cnt * rc
                out[lid]["jackpot"] += (eff_ch / 100.0) * avg_cnt * rj
            else:
                it = items.get(item_id)
                if not it or it["eff"] <= 0:
                    continue
                ev = (eff_ch / 100.0) * avg_cnt * it["eff"]
                if eff_ch < 0.5 and it["eff"] >= 200000:
                    out[lid]["jackpot"] += ev
                else:
                    out[lid]["direct"] += ev
                    if ev >= 150:  # >= 1.5s EV contribution
                        out[lid]["drops"].append((ev, it["name"], eff_ch, it["eff"], it["is_ah"]))

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Rank open-world WoW Forever gold farms by live AH EV/kill")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--min-level", type=int, default=38)
    ap.add_argument("--max-level", type=int, default=60)
    ap.add_argument("--min-spawns", type=int, default=12)
    ap.add_argument("--limit", type=int, default=35)
    ap.add_argument("--type", default="", help="Filter by creature type (e.g. Undead, Demon, Elemental, Beast, Humanoid)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    items = load_item_values(conn)
    ref_evs = compute_reference_evs(conn, items)
    loot_evs = compute_loot_evs(conn, items, ref_evs)

    rows = []
    for c in conn.execute(
        """
        SELECT entry, name, min_level, max_level, rank, creature_type,
               min_loot_gold, max_loot_gold, loot_id, spawn_count, primary_map
        FROM creatures
        WHERE rank = 0
          AND max_level BETWEEN ? AND ?
          AND primary_map IN (0, 1)
          AND spawn_count >= ?
        """,
        (args.min_level, args.max_level, args.min_spawns),
    ):
        ctype = CREATURE_TYPES.get(int(c["creature_type"]), "Other")
        if args.type and ctype.lower() != args.type.lower():
            continue
        coin_ev = (int(c["min_loot_gold"]) + int(c["max_loot_gold"])) / 2.0
        le = loot_evs.get(int(c["loot_id"]), {"direct": 0.0, "ref": 0.0, "jackpot": 0.0, "drops": []})
        consistent_ev = coin_ev + le["direct"] + le["ref"]
        top_drops = sorted(le["drops"], key=lambda x: x[0], reverse=True)[:3]
        drop_str = ", ".join(
            f"{nm} ({ch:.1f}% @ {val/10000:.2f}g{' AH' if is_ah else ' vend'})"
            for _, nm, ch, val, is_ah in top_drops
        )
        rows.append(
            {
                "entry": c["entry"],
                "name": c["name"],
                "lvl": f"{c['min_level']}-{c['max_level']}",
                "type": ctype,
                "map": MAPS.get(int(c["primary_map"]), "?"),
                "spawns": int(c["spawn_count"]),
                "coin_g": coin_ev / 10000.0,
                "direct_g": le["direct"] / 10000.0,
                "ref_g": le["ref"] / 10000.0,
                "total_g": consistent_ev / 10000.0,
                "drops": drop_str,
            }
        )

    rows.sort(key=lambda r: r["total_g"], reverse=True)
    print(f"Top {min(args.limit, len(rows))} Open-World Non-Elite Mobs (L{args.min_level}-{args.max_level}, spawns>={args.min_spawns}) by Live EV/kill:")
    print(f"{'Mob Name':<30} {'Lvl':<6} {'Type':<9} {'Cont':<3} {'Spwn':>4} | {'Total':>6} {'Coin':>5} {'Dir':>5} {'Ref':>5} | Key Drops")
    print("-" * 135)
    for r in rows[: args.limit]:
        cont = "EK" if r["map"] == "Eastern Kingdoms" else "Kal"
        print(
            f"{r['name']:<30} {r['lvl']:<6} {r['type']:<9} {cont:<3} {r['spawns']:>4} | "
            f"{r['total_g']:5.2f}g {r['coin_g']:4.2f}g {r['direct_g']:4.2f}g {r['ref_g']:4.2f}g | {r['drops']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
