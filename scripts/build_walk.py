#!/usr/bin/env python3.9
"""OSM から徒歩・自転車・自動車のグラフを作って SQLite に入れる。

    python3.9 scripts/build_walk.py [stage_id] [--buffer 5] [--pbf path]

Overpass は遅い（1 クエリ 40 秒〜）ので、地方版の pbf をローカルで切る。
  1. 区域の外接矩形＋バッファで osmium extract
  2. osmium tags-filter w/highway
  3. osmium cat -f osm.xml → 標準ライブラリでパース
  4. ノード・エッジを SQLite へ（1km 角のセル索引つき。探索時は周辺セルだけ読む）
"""
import argparse
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import stages as stages_mod  # noqa: E402
from backend.walk import CELL, SCHEMA, cell_of  # noqa: E402

GEOFABRIK = "https://download.geofabrik.de/asia/japan/{region}-latest.osm.pbf"
BUILD_DIR = Path(os.environ.get("KOSODATE_BUILD_DIR", "/var/tmp/kosodate-navi"))
SHARED_OSM = Path("/vagrant/source/odc2026/repos/data-exploration/data/osm")

# 歩ける／自転車で通れる／車で通れる道の種別
WALKABLE = {
    "footway", "path", "pedestrian", "steps", "living_street", "residential",
    "service", "unclassified", "tertiary", "tertiary_link", "secondary", "secondary_link",
    "primary", "primary_link", "track", "cycleway", "road", "busway",
}
CAR = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
    "residential", "living_street", "service", "road", "busway",
}
KEEP_TAGS = ("highway", "incline", "surface", "width", "bicycle", "foot", "sidewalk",
             "oneway", "covered", "tunnel", "handrail", "ramp", "ramp:stroller",
             "wheelchair", "maxspeed", "name", "lit", "crossing", "indoor", "layer")


def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p = math.pi / 180
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def find_pbf(region):
    for base in (SHARED_OSM, BUILD_DIR):
        p = base / f"{region}-latest.osm.pbf"
        if p.exists():
            return p
    out = BUILD_DIR / f"{region}-latest.osm.pbf"
    out.parent.mkdir(parents=True, exist_ok=True)
    url = GEOFABRIK.format(region=region)
    print(f"  取得: {url}（時間がかかります）")
    req = urllib.request.Request(url, headers={"User-Agent": "kosodate-navi"})
    with urllib.request.urlopen(req, timeout=3600) as res, out.open("wb") as f:
        shutil.copyfileobj(res, f)
    return out


def clip(stage, buffer_km, pbf):
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    x1, y1, x2, y2 = stage.bbox()
    d = buffer_km / 111.0
    dx = d / max(0.2, math.cos(math.radians((y1 + y2) / 2)))
    bbox = f"{x1 - dx:.4f},{y1 - d:.4f},{x2 + dx:.4f},{y2 + d:.4f}"
    cut = BUILD_DIR / f"{stage.id}.osm.pbf"
    hw = BUILD_DIR / f"{stage.id}_hw.osm.pbf"
    xml = BUILD_DIR / f"{stage.id}.osm"
    print(f"  bbox {bbox}")
    subprocess.run(["osmium", "extract", "-b", bbox, str(pbf), "-o", str(cut), "--overwrite"],
                   check=True)
    subprocess.run(["osmium", "tags-filter", str(cut), "w/highway", "-o", str(hw), "--overwrite"],
                   check=True)
    subprocess.run(["osmium", "cat", "-f", "osm.xml", str(hw), "-o", str(xml), "--overwrite"],
                   check=True)
    return xml


def parse(xml_path):
    nodes = {}
    node_tags = {}
    ways = []
    for _, el in ET.iterparse(str(xml_path), events=("end",)):
        if el.tag == "node":
            nid = int(el.get("id"))
            nodes[nid] = (float(el.get("lat")), float(el.get("lon")))
            tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
            if tags:
                keep = {k: v for k, v in tags.items()
                        if k in ("kerb", "highway", "barrier", "wheelchair", "traffic_signals")}
                if keep:
                    node_tags[nid] = keep
            el.clear()
        elif el.tag == "way":
            tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
            h = tags.get("highway")
            if h and (h in WALKABLE or h in CAR):
                refs = [int(nd.get("ref")) for nd in el.findall("nd")]
                ways.append((refs, {k: v for k, v in tags.items() if k in KEEP_TAGS}))
            el.clear()
    return nodes, node_tags, ways


def build(stage, buffer_km, pbf_path=None):
    region = stage.conf.get("pbf", "kanto")
    pbf = Path(pbf_path) if pbf_path else find_pbf(region)
    xml_path = clip(stage, buffer_km, pbf)
    print("  パース中…")
    nodes, node_tags, ways = parse(xml_path)
    print(f"  way {len(ways):,} / node {len(nodes):,}")

    db_tmp = BUILD_DIR / f"{stage.id}.walk.build.sqlite"
    if db_tmp.exists():
        db_tmp.unlink()
    con = sqlite3.connect(db_tmp)
    con.executescript(SCHEMA)

    used = set()
    edges = []
    for refs, tags in ways:
        h = tags.get("highway")
        walk_ok = h in WALKABLE and tags.get("foot") != "no"
        car_ok = h in CAR and tags.get("motor_vehicle") != "no"
        bike_ok = (h in WALKABLE and h != "steps" and tags.get("bicycle") != "no")
        oneway = tags.get("oneway") in ("yes", "1", "true")
        attrs = ";".join(f"{k}={v}" for k, v in tags.items() if k != "name" and v)
        name = tags.get("name") or ""
        for a, b in zip(refs, refs[1:]):
            if a not in nodes or b not in nodes:
                continue
            la1, lo1 = nodes[a]
            la2, lo2 = nodes[b]
            length = haversine(la1, lo1, la2, lo2)
            if length <= 0:
                continue
            used.add(a)
            used.add(b)
            edges.append((a, b, round(length, 2), h, attrs, name,
                          1 if walk_ok else 0, 1 if bike_ok else 0, 1 if car_ok else 0,
                          1 if oneway else 0, cell_of(la1, lo1)))
    con.executemany("INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?,?,?)", edges)
    con.executemany("INSERT INTO node VALUES (?,?,?,?,?)", [
        (nid, nodes[nid][0], nodes[nid][1],
         ";".join(f"{k}={v}" for k, v in node_tags.get(nid, {}).items()),
         cell_of(*nodes[nid]))
        for nid in used])
    con.executescript("""
        CREATE INDEX idx_edge_cell ON edge(cell);
        CREATE INDEX idx_edge_a ON edge(a);
        CREATE INDEX idx_node_cell ON node(cell);
    """)
    con.commit()
    con.close()

    out = stage.walk_db_path
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    shutil.copy(db_tmp, out)
    db_tmp.unlink()
    size = out.stat().st_size / 1024 / 1024
    print(f"  ✅ ノード {len(used):,} / エッジ {len(edges):,} / {size:.0f}MB → {out}")


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
