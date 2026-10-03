#!/usr/bin/env python3.9
"""**バス停の座標表を OpenStreetMap から作る。**

    python3.9 scripts/build_busstop_geo.py            # 全部の舞台
    python3.9 scripts/build_busstop_geo.py nagareyama

出力: `data/busstop/<stage>.json`（停留所名 → 座標の候補）

**なぜ要るか**: ODPT の**バスの JSON API には停留所の座標が入っていない**。
東武バスの `odpt:BusstopPole` は 4,728 件あるが、`geo:lat` / `geo:long` は**0 件**で、
名前・よみ・所属する系統しか持たない。GTFS のフィード（流山ぐりーんバスなど）には
`stops.txt` があるので座標に困らないが、JSON API しか無い事業者は自分で位置を補うしかない。

そこで、同じ場所の OSM の `highway=bus_stop` / `public_transport=platform` から
**名前で引ける座標表**を作っておき、`backend/odptbus.py` が JSON API を GTFS に
組み直すときに引く。名前は照合しやすいように正規化して持つ
（全角→半角、「（流山市）」のような補足を落とす、ヶ→ケ など）。

出典は OpenStreetMap contributors（ODbL）。
"""
import argparse
import collections
import json
import math
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend import stages as stages_mod          # noqa: E402
from backend.odptbus import norm_stop_name        # noqa: E402  （照合の規則は 1 か所に置く）
from build_walk import BUILD_DIR, find_pbf        # noqa: E402

OUT_DIR = stages_mod.REPO_ROOT / "data" / "busstop"


def clip(stage, buffer_km, pbf):
    """舞台の区域＋余白で切って、バス停だけの XML にする。"""
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    x1, y1, x2, y2 = stage.bbox()
    d = buffer_km / 111.0
    dx = d / max(0.2, math.cos(math.radians((y1 + y2) / 2)))
    bbox = f"{x1 - dx:.4f},{y1 - d:.4f},{x2 + dx:.4f},{y2 + d:.4f}"
    cut = BUILD_DIR / f"{stage.id}_bus_cut.osm.pbf"
    bus = BUILD_DIR / f"{stage.id}_busstop.osm.pbf"
    xml = BUILD_DIR / f"{stage.id}_busstop.osm"
    print(f"  bbox {bbox}")
    subprocess.run(["osmium", "extract", "-b", bbox, str(pbf), "-o", str(cut), "--overwrite"],
                   check=True)
    subprocess.run(["osmium", "tags-filter", str(cut), "n/highway=bus_stop",
                    "n/public_transport=platform", "-o", str(bus), "--overwrite"], check=True)
    subprocess.run(["osmium", "cat", "-f", "osm.xml", str(bus), "-o", str(xml), "--overwrite"],
                   check=True)
    return xml


def build(stage, buffer_km, pbf_path=None):
    region = stage.conf.get("pbf", "kanto")
    pbf = Path(pbf_path) if pbf_path else find_pbf(region)
    xml = clip(stage, buffer_km, pbf)

    table = collections.defaultdict(list)
    seen = set()
    for _, el in ET.iterparse(str(xml), events=("end",)):
        if el.tag != "node":
            continue
        tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
        name = tags.get("name")
        if name and (tags.get("highway") == "bus_stop"
                     or tags.get("public_transport") == "platform"):
            key = norm_stop_name(name)
            lat, lon = round(float(el.get("lat")), 6), round(float(el.get("lon")), 6)
            if (key, lat, lon) not in seen:
                seen.add((key, lat, lon))
                table[key].append([lat, lon, tags.get("operator") or ""])
        el.clear()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{stage.id}.json"
    out.write_text(json.dumps({"attribution": "© OpenStreetMap contributors",
                               "stops": table}, ensure_ascii=False), encoding="utf-8")
    poles = sum(len(v) for v in table.values())
    print(f"  停留所名 {len(table)} / 地点 {poles} → {out}"
          f"（{out.stat().st_size / 1024:.0f}KB）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", nargs="?")
    ap.add_argument("--buffer", type=float, default=6.0, help="区域の外側に取る余白（km）")
    ap.add_argument("--pbf")
    args = ap.parse_args()
    all_stages = stages_mod.load_all()
    targets = [all_stages[args.stage]] if args.stage else list(all_stages.values())
    for stage in targets:
        print(f"== {stage.name} ==")
        build(stage, args.buffer, args.pbf)


if __name__ == "__main__":
    main()
