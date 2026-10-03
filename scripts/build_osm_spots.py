#!/usr/bin/env python3.9
"""OpenStreetMap から、**公園の広さ**と**市外の行き先の候補**を作る。

    python3.9 scripts/build_osm_spots.py [stage_id]

出力: data/spots/generated/<stage>_park_polygons.json   ← 市内の公園の広さ（本番で使う）
      data/spots/generated/<stage>_osm_candidates.json  ← 市外の候補（人が選ぶための材料）

**2026-09-23: OSM のスポットを地図に出すのをやめた**（ユーザー指示「OSM にしか情報が
無い公園は落として、地図に出すスポットは web で情報を集めて」）。OSM にあるのは名前と形
だけで、遊具・トイレ・開園時間・公式ページが無く、**行き先として選べない**ため。

いまの OSM の役目は 2 つ。

- **公園の広さを測る**（オープンデータに広さの欄が無い。ポリゴンの面積を計算する）
- 市外の候補を面積つきで並べ、**人が「どの公園を調べるか」を決める材料にする**
  （調べた結果は data/spots/<stage>_outside.csv に手で書く → build_outside_spots.py）

- 範囲は**歩行グラフと同じ**（市域＋バッファ）。到達圏が届きうる範囲と一致させる
- 市内は市のオープンデータ＋自前整備のほうが詳しいので、**区域の中は入れない**
- 名前のあるものだけ入れる（名前のない公園は候補としても使えない）
- 市区町村名は国土数値情報の行政区域（N03）で判定する

**公園の広さもここで測る。** 公園のオープンデータ（市の CSV）は点だけで広さが無く、
429 件の公園を全部地図に出すと「わざわざ行く公園」と「近所の遊び場」が区別できない。
OSM は公園をポリゴンで持っているので、その面積を測って両方に配る。

- 市外のスポットには `area_m2` を直接付ける
- **市内のポリゴンは `<stage>_park_polygons.json` に書き出す**。
  `build_spots.py` が市の公園の点を包むポリゴンを探して広さを付ける
  （そのため build_osm_spots.py → build_spots.py の順に流す）
"""
import argparse
import json
import math
import os
import subprocess
import sys
import sqlite3
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import stages as stages_mod  # noqa: E402

BUILD_DIR = Path(os.environ.get("KOSODATE_BUILD_DIR", "/var/tmp/kosodate-navi"))
SHARED_OSM = Path("/vagrant/source/odc2026/repos/data-exploration/data/osm")
N03_URL = "https://nlftp.mlit.go.jp/ksj/gml/data/N03/N03-2025/N03-20250101_{pref}_GML.zip"

# OSM のタグ → この作品の施設の種類。**今日ふらっと行ける場所**だけを拾う
TAG_TYPES = [
    (("leisure", "playground"), "公園"),
    (("leisure", "park"), "公園"),
    (("amenity", "library"), "図書館"),
    (("amenity", "community_centre"), "公民館"),
]
FILTERS = ["nwr/leisure=playground,park", "nwr/amenity=library,community_centre",
           "nwr/changing_table=yes"]
# 周辺の県。到達圏がまたぐ可能性のあるところだけ
NEIGHBOR_PREFS = {"nagareyama": ["12", "11", "13", "08"]}


def walk_bbox(stage, margin=0.005):
    """歩行グラフが持っている範囲。到達圏が届きうる範囲と同じにする。"""
    if not stage.walk_db_path.exists():
        x1, y1, x2, y2 = stage.bbox()
        return (x1 - 0.05, y1 - 0.05, x2 + 0.05, y2 + 0.05)
    con = sqlite3.connect(f"file:{stage.walk_db_path}?mode=ro", uri=True)
    lat1, lat2, lon1, lon2 = con.execute(
        "SELECT min(lat), max(lat), min(lon), max(lon) FROM node").fetchone()
    con.close()
    return (lon1 - margin, lat1 - margin, lon2 + margin, lat2 + margin)


def find_pbf(region):
    for base in (SHARED_OSM, BUILD_DIR):
        p = base / f"{region}-latest.osm.pbf"
        if p.exists():
            return p
    raise SystemExit(f"{region} の pbf が見つかりません（build_walk.py で取得されます）")


def extract(stage, bbox):
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    pbf = find_pbf(stage.conf.get("pbf", "kanto"))
    area = BUILD_DIR / f"{stage.id}_area.osm.pbf"
    poi = BUILD_DIR / f"{stage.id}_poi.osm.pbf"
    out = BUILD_DIR / f"{stage.id}_poi.geojsonseq"
    box = ",".join(f"{v:.4f}" for v in bbox)
    if not area.exists():
        print(f"  osmium extract -b {box}（数十秒かかります）")
        subprocess.run(["osmium", "extract", "-b", box, str(pbf), "-o", str(area),
                        "--overwrite"], check=True)
    subprocess.run(["osmium", "tags-filter", str(area)] + FILTERS
                   + ["-o", str(poi), "--overwrite"], check=True)
    subprocess.run(["osmium", "export", "-f", "geojsonseq",
                    "--geometry-types=point,polygon", str(poi), "-o", str(out),
                    "--overwrite"], check=True)
    return out


def polygon_rings(geom):
    """外周のリングだけ取り出す（穴は面積に効くほど大きくないので見ない）。"""
    if geom["type"] == "Polygon":
        return [geom["coordinates"][0]]
    if geom["type"] == "MultiPolygon":
        return [poly[0] for poly in geom["coordinates"]]
    return []


def ring_area_m2(ring):
    """リングの面積（m²）。緯度で経度を縮めた平面近似で足りる（公園の桁を知りたいだけ）。"""
    if len(ring) < 4:
        return 0.0
    lat0 = sum(q[1] for q in ring) / len(ring)
    mx = 111320.0 * math.cos(math.radians(lat0))
    my = 110540.0
    s = 0.0
    for i in range(len(ring) - 1):
        s += (ring[i][0] * mx) * (ring[i + 1][1] * my) \
            - (ring[i + 1][0] * mx) * (ring[i][1] * my)
    return abs(s) / 2


def centroid(geom):
    if geom["type"] == "Point":
        return geom["coordinates"][1], geom["coordinates"][0]
    ring = geom["coordinates"][0]
    if geom["type"] == "MultiPolygon":
        ring = geom["coordinates"][0][0]
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return sum(ys) / len(ys), sum(xs) / len(xs)


def load_municipalities(pref_codes):
    """行政区域から市区町村のポリゴンを読む。スポットに「どこの市か」を付けるため。"""
    out = []
    for pref in pref_codes:
        path = stages_mod.RAW / "boundary" / f"N03_{pref}.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            url = N03_URL.format(pref=pref)
            print(f"  取得: {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "kosodate-navi"})
            with urllib.request.urlopen(req, timeout=600) as res:
                path.write_bytes(res.read())
        with zipfile.ZipFile(path) as zf:
            member = [n for n in zf.namelist() if n.endswith(".geojson")][0]
            geo = json.loads(zf.read(member).decode("utf-8"))
        for f in geo["features"]:
            p = f["properties"]
            name = (p.get("N03_004") or p.get("N03_003") or "").strip()
            if not name:
                continue
            g = f["geometry"]
            polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
            for poly in polys:
                ring = poly[0]
                xs = [q[0] for q in ring]
                ys = [q[1] for q in ring]
                out.append((min(xs), min(ys), max(xs), max(ys), ring,
                            (p.get("N03_003") or "") + name))
    return out


def in_ring(lat, lon, ring):
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if (y1 > lat) != (y2 > lat):
            xx = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < xx:
                inside = not inside
    return inside


def city_of(lat, lon, munis):
    for x1, y1, x2, y2, ring, name in munis:
        if x1 <= lon <= x2 and y1 <= lat <= y2 and in_ring(lat, lon, ring):
            return name
    return None


def facility_type_of(props):
    for (key, value), label in TAG_TYPES:
        if props.get(key) == value:
            return label
    if props.get("changing_table") == "yes":
        return "おむつ替えのできるトイレ"
    return None


def build(stage):
    bbox = walk_bbox(stage)
    path = extract(stage, bbox)
    munis = load_municipalities(NEIGHBOR_PREFS.get(stage.id, [stage.pref_code]))
    print(f"  市区町村ポリゴン {len(munis)}")

    spots, skipped_inside, unnamed = [], 0, 0
    inside_polys = []
    seen = set()
    for line in open(path, encoding="utf-8"):
        line = line.strip().lstrip("\x1e")
        if not line:
            continue
        f = json.loads(line)
        props = f["properties"]
        name = (props.get("name") or "").strip()
        ftype = facility_type_of(props)
        if not ftype:
            continue
        rings = polygon_rings(f["geometry"]) if ftype == "公園" else []
        area_m2 = int(sum(ring_area_m2(r) for r in rings)) if rings else None
        lat, lon = centroid(f["geometry"])
        if stage.contains(lat, lon):
            skipped_inside += 1          # 市内は市のオープンデータのほうが詳しい
            if rings and area_m2:
                # **広さだけはもらう。** 市の CSV は点しか持っていないので、
                # build_spots.py がこのポリゴンで市の公園に広さを付ける
                for ring in rings:
                    xs = [q[0] for q in ring]
                    ys = [q[1] for q in ring]
                    inside_polys.append({
                        "name": name, "area_m2": int(ring_area_m2(ring)),
                        "bbox": [round(min(xs), 6), round(min(ys), 6),
                                 round(max(xs), 6), round(max(ys), 6)],
                        "ring": [[round(q[0], 6), round(q[1], 6)] for q in ring]})
            continue
        if not name:
            unnamed += 1
            continue
        key = (name, round(lat, 4), round(lon, 4))
        if key in seen:
            continue
        seen.add(key)
        city = city_of(lat, lon, munis)
        osm_id = f.get("id") or props.get("@id") or ""
        spot = {
            "id": f"osm:{osm_id or len(spots)}",
            "name": name,
            "lat": round(lat, 6), "lon": round(lon, 6),
            "category": ftype,
            "facility_type": ftype,
            "dropin": True,
            "visit_note": "予約なしで、その日に行けます",
            "age_min": 0, "age_max": 155,
            "open_time": None, "close_time": None,
            "closed_days": None,
            "fee_text": None, "fee_free": None,
            "stroller_ok": None, "nursing_room": None,
            "diaper_table": True if props.get("changing_table") == "yes" else None,
            "hot_water": None,
            "indoor": ftype in ("図書館", "公民館"),
            "capacity": None,
            "note": props.get("opening_hours") or None,
            "city": city,
            "outside": True,
            "source_id": "osm",
            "source_label": "OpenStreetMap",
            "source_url": (f"https://www.openstreetmap.org/{osm_id}"
                           if osm_id else "https://www.openstreetmap.org/"),
            "checked_at": None,
        }
        if area_m2:
            spot["area_m2"] = area_m2
        if props.get("website"):
            spot["official_url"] = props["website"]
            spot["official_label"] = "公式ホームページ（OSM の website タグ）"
        spots.append(spot)

    by_city = {}
    for s in spots:
        by_city[s["city"] or "（不明）"] = by_city.get(s["city"] or "（不明）", 0) + 1
    out = {"stage": stage.id, "bbox": bbox, "spots": spots,
           "by_city": sorted(by_city.items(), key=lambda x: -x[1]),
           "skipped_inside": skipped_inside, "unnamed": unnamed}
    gen = stages_mod.REPO_ROOT / "data" / "spots" / "generated"
    dest = gen / f"{stage.id}_osm_candidates.json"
    dest.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"  市外の候補 {len(spots)} 件（市内で採らなかったもの {skipped_inside} 件／"
          f"名前が無くて採らなかったもの {unnamed} 件）→ {dest}")
    print("  ※ ここから先は人が選ぶ。広さの大きい順に公式ページを当たり、"
          f"data/spots/{stage.id}_outside.csv に書く")
    poly_dest = gen / f"{stage.id}_park_polygons.json"
    poly_dest.write_text(json.dumps({"stage": stage.id, "polygons": inside_polys},
                                    ensure_ascii=False), encoding="utf-8")
    sized = sum(1 for s in spots if s.get("area_m2"))
    print(f"  公園の広さ: 市外 {sized} 件に直接付けた／"
          f"市内のポリゴン {len(inside_polys)} 件 → {poly_dest}")
    for city, n in out["by_city"][:12]:
        print(f"    {city}: {n}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", nargs="?", default="nagareyama")
    args = ap.parse_args()
    build(stages_mod.load(args.stage))
