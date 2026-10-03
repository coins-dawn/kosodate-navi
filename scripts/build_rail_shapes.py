#!/usr/bin/env python3.9
"""**線路の形を OpenStreetMap から作る。**

    python3.9 scripts/build_rail_shapes.py            # 全部の舞台
    python3.9 scripts/build_rail_shapes.py nagareyama

出力: `data/rail/<stage>.geojson`（路線ごとの LineString）

**なぜ要るか**: 地図に出す電車の線が、駅と駅を結んだ**直線**になっていた。
GTFS には便ごとの形（`shapes.txt`）が入れられることになっていて、
流山ぐりーんバスは出しているが、**つくばエクスプレスの GTFS には shapes.txt が無い**
（agency / calendar / routes / stop_times / stops / trips / translations / fare だけ）。
時刻表からは線路の曲がりが分からないので、**同じ場所の OSM から線路そのものを取る**。

やっていること:

1. 歩行グラフと同じ地方版 pbf を、舞台の区域＋余白で切る
2. `railway=rail / light_rail / subway / monorail / tram` の way を拾う。
   **`service` の付いた way（車庫・待避線・渡り線）は捨てる**（本線だけ欲しい）
3. 端点を共有する way をつないで、**路線ごとに 1 本の長い線**にする
   （複線は上り・下りで 2 本になる。どちらを使っても地図の上では 5m と違わない）
4. 名前・運行者・長さを付けて GeoJSON に書く

出典は OpenStreetMap contributors（ODbL）。画面の地図の出典にすでに入れている。
"""
import argparse
import json
import math
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend import stages as stages_mod      # noqa: E402
from build_walk import BUILD_DIR, find_pbf    # noqa: E402  （pbf の置き場を共有する）

RAIL_KINDS = {"rail", "light_rail", "subway", "monorail", "tram"}
OUT_DIR = stages_mod.REPO_ROOT / "data" / "rail"
MIN_LENGTH_M = 300          # これより短い切れ端は捨てる（構内の枝など）


def haversine(a, b):
    (x1, y1), (x2, y2) = a, b
    p1, p2 = math.radians(y1), math.radians(y2)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(x2 - x1) / 2) ** 2)
    return 2 * 6371000.0 * math.asin(math.sqrt(h))


def clip(stage, buffer_km, pbf):
    """舞台の区域＋余白で切って、線路だけの XML にする。"""
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    x1, y1, x2, y2 = stage.bbox()
    d = buffer_km / 111.0
    dx = d / max(0.2, math.cos(math.radians((y1 + y2) / 2)))
    bbox = f"{x1 - dx:.4f},{y1 - d:.4f},{x2 + dx:.4f},{y2 + d:.4f}"
    cut = BUILD_DIR / f"{stage.id}_rail_cut.osm.pbf"
    rail = BUILD_DIR / f"{stage.id}_rail.osm.pbf"
    xml = BUILD_DIR / f"{stage.id}_rail.osm"
    print(f"  bbox {bbox}")
    subprocess.run(["osmium", "extract", "-b", bbox, str(pbf), "-o", str(cut), "--overwrite"],
                   check=True)
    subprocess.run(["osmium", "tags-filter", str(cut), "w/railway",
                    "-o", str(rail), "--overwrite"], check=True)
    subprocess.run(["osmium", "cat", "-f", "osm.xml", str(rail), "-o", str(xml), "--overwrite"],
                   check=True)
    return xml


def parse(xml_path):
    nodes, ways = {}, []
    for _, el in ET.iterparse(str(xml_path), events=("end",)):
        if el.tag == "node":
            nodes[int(el.get("id"))] = (float(el.get("lon")), float(el.get("lat")))
        elif el.tag == "way":
            tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
            # service（車庫・待避線・渡り線）は本線ではないので捨てる
            if tags.get("railway") in RAIL_KINDS and not tags.get("service"):
                refs = [int(n.get("ref")) for n in el.findall("nd")]
                if len(refs) >= 2:
                    ways.append((refs, tags))
            el.clear()
    return nodes, ways


def stitch(ways):
    """端点を共有する way をつないで、長い線にする。

    OSM の way は信号や橋のたびに切れている。つなぐと路線 1 本ぶんになり、
    駅から駅までを**切り出せる**ようになる（切れたままだと拾えない）。
    """
    refs = [list(r) for r, _ in ways]
    used, chains = set(), []
    for i in range(len(refs)):
        if i in used:
            continue
        cur = list(refs[i])
        used.add(i)
        changed = True
        while changed:
            changed = False
            for j, r in enumerate(refs):
                if j in used:
                    continue
                if r[0] == cur[-1]:
                    cur += r[1:]
                elif r[-1] == cur[-1]:
                    cur += r[-2::-1]
                elif r[-1] == cur[0]:
                    cur = r[:-1] + cur
                elif r[0] == cur[0]:
                    cur = r[:0:-1] + cur
                else:
                    continue
                used.add(j)
                changed = True
        chains.append(cur)
    return chains


def build(stage, buffer_km, pbf_path=None):
    region = stage.conf.get("pbf", "kanto")
    pbf = Path(pbf_path) if pbf_path else find_pbf(region)
    nodes, ways = parse(clip(stage, buffer_km, pbf))
    print(f"  線路の way {len(ways)} 本")

    by_line = defaultdict(list)
    for refs, tags in ways:
        by_line[(tags.get("name") or "", tags.get("operator") or "")].append((refs, tags))

    feats = []
    for (name, operator), group in sorted(by_line.items()):
        if not name:
            continue                      # 名前の無い線は、路線に結びつけられない
        for chain in stitch(group):
            pts = [nodes[n] for n in chain if n in nodes]
            if len(pts) < 2:
                continue
            length = sum(haversine(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
            if length < MIN_LENGTH_M:
                continue
            feats.append({
                "type": "Feature",
                "properties": {"name": name, "operator": operator,
                               "length_m": round(length)},
                "geometry": {"type": "LineString",
                             "coordinates": [[round(x, 6), round(y, 6)] for x, y in pts]},
            })
    feats.sort(key=lambda f: (-f["properties"]["length_m"], f["properties"]["name"]))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{stage.id}.geojson"
    out.write_text(json.dumps({
        "type": "FeatureCollection",
        "attribution": "© OpenStreetMap contributors",
        "features": feats}, ensure_ascii=False), encoding="utf-8")
    for f in feats:
        p = f["properties"]
        print(f"    {p['name']}（{p['operator']}）{p['length_m'] / 1000:.1f}km "
              f"/ {len(f['geometry']['coordinates'])}点")
    print(f"  {len(feats)} 本 → {out}（{out.stat().st_size / 1024:.0f}KB）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", nargs="?")
    ap.add_argument("--buffer", type=float, default=5.0, help="区域の外側に取る余白（km）")
    ap.add_argument("--pbf")
    args = ap.parse_args()
    all_stages = stages_mod.load_all()
    targets = [all_stages[args.stage]] if args.stage else list(all_stages.values())
    for stage in targets:
        print(f"== {stage.name} ==")
        build(stage, args.buffer, args.pbf)


if __name__ == "__main__":
    main()
