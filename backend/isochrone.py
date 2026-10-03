"""到達圏（自宅から N 分で行ける範囲）。

**この作品の見せ方の中心**。自宅と時間の上限を決めると、
**徒歩・自転車・徒歩＋公共交通の 3 つの到達圏が同時に**地図に出て、
そのどれかに入るスポットだけが残る。手段を 1 つ選ばせないのは、
「歩きならここまで、自転車なら、バスに乗るなら」を並べて見せたいため。

- 徒歩・ベビーカー: 歩行グラフのダイクストラ。ベビーカーは階段を通れないので形が変わる
- 自転車: 同じグラフを `graph_mode="bike"` で通る（自転車が入れない道は落ち、速さが変わる）
- 公共交通: 時刻表探索（RAPTOR）で着ける停留所・駅を出し、**残り時間**で各駅から歩く
  （多点ダイクストラ）。乗れる便は出発時刻で変わるので、日付と時刻を受け取る

面は**100m 前後のマス目の集まり**として持つ。地図に描く形と、スポットが入るかの判定が
同じものになるので、「塗られているのにスポットが消える」ような食い違いが起きない。
"""
import heapq
import math
from datetime import date

CELL_BASE_M = 100          # マスの大きさ。時間の上限が大きいときは粗くする
SEED_SNAP_M = 300          # 停留所から歩行グラフに乗り移れる距離
MAX_MINUTES = 60


def fill_enclosed(cells):
    """**まわりを囲まれた空きマスを埋める。**

    マスは「道が通っているところ」から作るので、家の建て込んだ区画や私有地の
    まん中は道が無く、面に**ぽつぽつと穴**が開く。塗ると穴だらけで読みにくい。

    外側から塗りつぶして「外につながっていない空きマス」を探し、それを埋める。
    **湾のように外へつながっている凹みは残る**（そこは本当に届かない場所なので
    埋めてはいけない）。斜めのつながりは通さないので、細い切れ込みも埋まる。

    **埋めた結果をそのままマスの集合として持つ**のが要点。地図に描く形と
    `contains()` の判定が同じものになるので、「塗られているのにスポットが消える」
    食い違いが起きない（そのぶん `area_km2` は穴の分だけ増える）。
    """
    if not cells:
        return cells
    ys = [c[0] for c in cells]
    xs = [c[1] for c in cells]
    y0, y1 = min(ys) - 1, max(ys) + 1
    x0, x1 = min(xs) - 1, max(xs) + 1
    outside = set()
    stack = [(y0, x0)]
    while stack:
        cur = stack.pop()
        if cur in outside or cur in cells:
            continue
        y, x = cur
        if not (y0 <= y <= y1 and x0 <= x <= x1):
            continue
        outside.add(cur)
        stack.append((y + 1, x))
        stack.append((y - 1, x))
        stack.append((y, x + 1))
        stack.append((y, x - 1))
    filled = set(cells)
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            if (y, x) not in cells and (y, x) not in outside:
                filled.add((y, x))
    return filled


def cell_size_for(minutes):
    if minutes <= 20:
        return CELL_BASE_M
    if minutes <= 40:
        return CELL_BASE_M * 1.5
    return CELL_BASE_M * 2


class Reach:
    """到達圏。マス目の集合と、その外形（GeoJSON）を持つ。"""

    def __init__(self, origin, mode, minutes, cell_m, cells, stats=None, label=None):
        self.origin = origin
        self.mode = mode
        self.label = label or mode
        self.minutes = minutes
        self.cell_m = cell_m
        self.cells = cells                       # {(y, x)}
        self.dlat = cell_m / 111000.0
        self.dlon = cell_m / (111000.0 * math.cos(math.radians(origin[0])))
        self.stats = stats or {}
        self._geojson = None

    def cell_of(self, lat, lon):
        return (int(math.floor(lat / self.dlat)), int(math.floor(lon / self.dlon)))

    def contains(self, lat, lon):
        """スポットがこの到達圏に入るか。**隣のマスまで**許す（道の端に立つ施設のため）。"""
        y, x = self.cell_of(lat, lon)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if (y + dy, x + dx) in self.cells:
                    return True
        return False

    def area_km2(self):
        return len(self.cells) * (self.cell_m / 1000.0) ** 2

    def bbox(self):
        if not self.cells:
            return None
        ys = [c[0] for c in self.cells]
        xs = [c[1] for c in self.cells]
        return [min(xs) * self.dlon, min(ys) * self.dlat,
                (max(xs) + 1) * self.dlon, (max(ys) + 1) * self.dlat]

    def geojson(self):
        """マスをつないだ外形。shapely があればきれいにつなぎ、無ければマスのまま出す。"""
        if self._geojson is not None:
            return self._geojson
        geom = None
        try:
            from shapely.geometry import box
            from shapely.ops import unary_union
            merged = unary_union([box(x * self.dlon, y * self.dlat,
                                      (x + 1) * self.dlon, (y + 1) * self.dlat)
                                  for y, x in self.cells])
            merged = merged.simplify(self.dlat / 4)
            geom = merged.__geo_interface__
        except ImportError:
            polys = [[[[x * self.dlon, y * self.dlat],
                       [(x + 1) * self.dlon, y * self.dlat],
                       [(x + 1) * self.dlon, (y + 1) * self.dlat],
                       [x * self.dlon, (y + 1) * self.dlat],
                       [x * self.dlon, y * self.dlat]]] for y, x in self.cells]
            geom = {"type": "MultiPolygon", "coordinates": polys}
        self._geojson = {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "geometry": geom,
                          "properties": {"mode": self.mode, "label": self.label,
                                         "minutes": self.minutes,
                                         "area_km2": round(self.area_km2(), 2)}}],
        }
        return self._geojson

    def to_json(self, include_geometry=True):
        out = {"mode": self.mode, "label": self.label, "minutes": self.minutes,
               "cell_m": int(self.cell_m), "cells": len(self.cells),
               "area_km2": round(self.area_km2(), 2),
               "origin": {"lat": self.origin[0], "lon": self.origin[1]}}
        out.update(self.stats)
        if include_geometry:
            out["geojson"] = self.geojson()
        return out


def overlap_zones(reaches):
    """**重なりを、重なり方ごとの互いに重ならない面に切り分ける。**

    3 つの到達圏をそのまま半透明で重ねると、重なった場所の色が描く順番で変わってしまう。
    面をあらかじめ切り分けておけば、**どこがどの組み合わせで届くのかを 1 色ずつ**塗れる。

    同じ時間の上限・同じ自宅なら 3 つともマス目が揃う（`cell_size_for` は分数だけで決まる）
    ので、セルの集合演算だけで切り分けられる。
    """
    reaches = [r for r in reaches if r.cells]
    if not reaches:
        return []
    order = [r.mode for r in reaches]
    by_cell = {}
    for r in reaches:
        for cell in r.cells:
            by_cell.setdefault(cell, []).append(r.mode)
    groups = {}
    for cell, modes in by_cell.items():
        key = tuple(m for m in order if m in modes)
        groups.setdefault(key, set()).add(cell)
    base = reaches[0]
    out = []
    for key, cells in groups.items():
        zone = Reach(base.origin, "+".join(key), base.minutes, base.cell_m, cells,
                     label="・".join(key))
        out.append({"modes": list(key), "area_km2": round(zone.area_km2(), 2),
                    "geojson": zone.geojson()})
    out.sort(key=lambda z: -z["area_km2"])
    return out


class IsochroneService:
    """舞台ごとに 1 つ。計算した到達圏は使い回す（自宅は候補から選ぶので当たりやすい）。"""

    def __init__(self, walk_graph, router=None, cache_size=48):
        self.walk = walk_graph
        self.router = router
        self._cache = {}
        self._order = []
        self.cache_size = cache_size

    def compute(self, origin, profile, minutes, when=None, on_date=None):
        minutes = max(5, min(int(minutes), MAX_MINUTES))
        key = (round(origin[0], 5), round(origin[1], 5), profile.id, profile.graph_mode, minutes,
               (when // 900 if profile.transit and when is not None else None),
               (on_date.isoformat() if profile.transit and on_date else None))
        if key in self._cache:
            return self._cache[key]
        if profile.transit:
            reach = self._transit(origin, profile, minutes, when or 9 * 3600,
                                  on_date or date.today())
        else:
            reach = self._ground(origin, profile, minutes)
        self._cache[key] = reach
        self._order.append(key)
        while len(self._order) > self.cache_size:
            self._cache.pop(self._order.pop(0), None)
        return reach

    # --- 徒歩・ベビーカー・自転車 ---

    def _ground(self, origin, profile, minutes):
        """自分の足だけで広がる到達圏。`profile.graph_mode` で徒歩と自転車を切り替える。"""
        budget = minutes * 60
        cell_m = cell_size_for(minutes)
        if not (self.walk and self.walk.available):
            return Reach(origin, profile.id, minutes, cell_m, set(),
                         {"reached_nodes": 0, "note": "歩行グラフがありません"},
                         label=profile.label)
        reach_m = budget / 60.0 * profile.graph_speed + 300
        self.walk.load_area([origin], min(reach_m, 15000))
        dist = self._multi_dijkstra({}, profile, budget, seeds=[(origin[0], origin[1], 0)],
                                    graph_mode=profile.graph_mode)
        return self._to_reach(origin, profile, minutes, cell_m, dist,
                              {"reached_nodes": len(dist)})

    # --- 公共交通 ---

    def _transit(self, origin, profile, minutes, when, on_date):
        budget = minutes * 60
        cell_m = cell_size_for(minutes)
        seeds = [(origin[0], origin[1], 0)]
        places = []
        if self.router is not None:
            arrivals = self.router.reachable_places(
                (origin[0], origin[1], "自宅"), when, profile, budget, on_date=on_date)
            for pid, t in arrivals.items():
                place = self.router.net.places[pid]
                seeds.append((place["lat"], place["lon"], t - when))
                places.append({"id": pid, "name": place["name"],
                               "lat": place["lat"], "lon": place["lon"],
                               "minutes": int((t - when) / 60)})
        if not (self.walk and self.walk.available):
            return Reach(origin, profile.id, minutes, cell_m, set(),
                         {"reached_nodes": 0, "places": len(places)}, label=profile.label)
        # 停留所ごとに「残り時間で歩ける距離」だけ読む
        pts = [(s[0], s[1]) for s in seeds]
        radius = max((budget - s[2]) for s in seeds) / 60.0 * profile.walk_speed + 300
        self.walk.load_area(pts, min(radius, 15000))
        dist = self._multi_dijkstra({}, profile, budget, seeds=seeds)
        places.sort(key=lambda p: p["minutes"])
        # **停留所の一覧そのものは返さない**（画面は「N か所」という数しか使わない）。
        # 時刻表データの中身（停留所名と座標）を、成果物の表示に要らないのに
        # 配ってしまわないため（公共交通オープンデータ基本ライセンス 第4条2項(3)）
        return self._to_reach(origin, profile, minutes, cell_m, dist,
                              {"reached_nodes": len(dist), "places": len(places)})

    # --- 共通 ---

    def _multi_dijkstra(self, dist, profile, budget, seeds, graph_mode="walk"):
        """複数の出発点（自宅と、着ける停留所）から同時に広げる。

        `graph_mode` は歩行グラフをどの手段で通るか。自転車では通れない道が落ち、
        道に乗り移るまでの速さも変わる。乗り継ぎの脚（公共交通）は徒歩のまま。
        """
        pq = []
        speed = profile.graph_speed if graph_mode == "bike" else profile.walk_speed
        for lat, lon, used in seeds:
            if used > budget:
                continue
            nid, d = self.walk.nearest_node(lat, lon, max_m=SEED_SNAP_M, mode=graph_mode)
            if nid is None:
                continue
            cost = used + d / speed * 60
            if cost <= budget and cost < dist.get(nid, float("inf")):
                dist[nid] = cost
                heapq.heappush(pq, (cost, nid))
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, float("inf")):
                continue
            for v, e in self.walk.adj.get(u, []):
                cost = profile.edge_cost(e, graph_mode, self.walk.node_tags.get(v, {}))
                if cost is None:
                    continue
                nd = d + cost
                if nd <= budget and nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return dist

    def _to_reach(self, origin, profile, minutes, cell_m, dist, stats):
        dlat = cell_m / 111000.0
        dlon = cell_m / (111000.0 * math.cos(math.radians(origin[0])))
        cells = set()
        for nid in dist:
            lat, lon = self.walk.nodes[nid]
            cells.add((int(math.floor(lat / dlat)), int(math.floor(lon / dlon))))
        # 自宅のマスは必ず入れる（家の前が私道で道に乗れないときでも空にしない）
        cells.add((int(math.floor(origin[0] / dlat)), int(math.floor(origin[1] / dlon))))
        # 囲まれた空きマス（道の無い区画）を埋めて、面に穴が開かないようにする
        cells = fill_enclosed(cells)
        return Reach(origin, profile.id, minutes, cell_m, cells, stats, label=profile.label)
