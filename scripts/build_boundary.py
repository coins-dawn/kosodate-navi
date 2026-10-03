#!/usr/bin/env python3.9
"""国土数値情報 行政区域（N03）から舞台の輪郭を切り出す。

    python3.9 scripts/build_boundary.py [stage_id]

地図で「区域の外を薄くする」のと、自宅を区域内に限るのに使う。
建物単位の精度は要らないので Douglas-Peucker で間引く。
"""
import json
import math
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import stages as stages_mod  # noqa: E402

N03_URL = "https://nlftp.mlit.go.jp/ksj/gml/data/N03/N03-2025/N03-20250101_{pref}_GML.zip"
TOLERANCE = 0.00007      # 度。おおよそ 7m
MIN_AREA_M2 = 1000


def simplify(points, tol):
    if len(points) < 3:
        return points
    first, last = points[0], points[-1]
    dmax, index = 0.0, 0
    for i in range(1, len(points) - 1):
        d = _perp(points[i], first, last)
        if d > dmax:
            dmax, index = d, i
    if dmax > tol:
        left = simplify(points[:index + 1], tol)
        right = simplify(points[index:], tol)
        return left[:-1] + right
    return [first, last]


def _perp(p, a, b):
    if a == b:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def ring_area_m2(ring, lat):
    s = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2 * (111000.0 ** 2) * math.cos(math.radians(lat))


def build(stage):
    import urllib.request
    zip_path = stages_mod.RAW / "boundary" / f"N03_{stage.pref_code}.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if not zip_path.exists():
        url = N03_URL.format(pref=stage.pref_code)
        print(f"  取得: {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "kosodate-navi"})
        with urllib.request.urlopen(req, timeout=600) as res:
            zip_path.write_bytes(res.read())

    with zipfile.ZipFile(zip_path) as zf:
        member = [n for n in zf.namelist() if n.endswith(".geojson")][0]
        geo = json.loads(zf.read(member).decode("utf-8"))

    rings = []
    for f in geo["features"]:
        if (f["properties"].get("N03_007") or "") != stage.code:
            continue
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        for poly in polys:
            rings.append(poly[0])

    lat = stage.center.get("lat", 35.7)
    kept = []
    for ring in rings:
        if ring_area_m2(ring, lat) < MIN_AREA_M2:
            continue
        simple = simplify(ring, TOLERANCE)
        if simple[0] != simple[-1]:
            simple.append(simple[0])
        kept.append(simple)

    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"code": stage.code, "name": stage.name},
         "geometry": {"type": "Polygon", "coordinates": [r]}} for r in kept]}
    stage.boundary_path.parent.mkdir(parents=True, exist_ok=True)
    stage.boundary_path.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    stage._boundary = None
    print(f"{stage.name}: ポリゴン {len(rings)} → {len(kept)}、面積 {stage.area_km2():.1f}km²、"
          f"{stage.boundary_path.stat().st_size // 1024}KB")


def main():
    all_stages = stages_mod.load_all()
    targets = [all_stages[sys.argv[1]]] if len(sys.argv) > 1 else list(all_stages.values())
    for stage in targets:
        build(stage)


if __name__ == "__main__":
    main()
