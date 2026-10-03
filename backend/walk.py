"""徒歩・自転車・自動車のグラフと探索。

OSM から作ったノード・エッジを SQLite に持ち、**探索のたびに周辺のセルだけ読む**。
10km 圏ぜんぶをメモリに載せると重いが、歩く区間は短いのでこれで足りる。

コストは backend/profile.py の重みで決める。年齢で「通れない道」が変わるのがこの作品の肝で、
0 歳（ベビーカー）では階段を通行不可にする。
"""
import heapq
import math
import sqlite3
from pathlib import Path

CELL = 0.01          # セルの大きさ（度）。およそ 1.1km × 0.9km
WALK_SPEED = 80.0    # m/分
BIKE_SPEED = 220.0
CAR_SPEED = 330.0

# --- その道に何があるか（当て馬の道を見せるための印）---
#
# **道を選ぶのには使わない。** 選ぶのは profile.edge_cost の重み。ここで作るのは
# 「段差をよけない道には、こういうものがある」と**地図の上で名指しするための印**だけ。
LEV_DIFF_LABEL = {"3": "2〜5cm の段差", "4": "5〜10cm の段差", "5": "10cm を超える段差"}
MERGE_M = 45         # 同じ種類の印がこれより近ければ 1 つにまとめる（階段は辺が細切れ）

SCHEMA = """
CREATE TABLE node (id INTEGER PRIMARY KEY, lat REAL, lon REAL, tags TEXT, cell TEXT);
CREATE TABLE edge (
  a INTEGER, b INTEGER, length REAL, highway TEXT, attrs TEXT, name TEXT,
  walk INTEGER, bike INTEGER, car INTEGER, oneway INTEGER, cell TEXT
);
"""


def cell_of(lat, lon):
    return f"{int(math.floor(lat / CELL))}_{int(math.floor(lon / CELL))}"


def cells_around(lat, lon, radius_m):
    d = radius_m / 111000.0
    dx = d / max(0.2, math.cos(math.radians(lat)))
    out = []
    y = math.floor((lat - d) / CELL)
    while y <= math.floor((lat + d) / CELL):
        x = math.floor((lon - dx) / CELL)
        while x <= math.floor((lon + dx) / CELL):
            out.append(f"{int(y)}_{int(x)}")
            x += 1
        y += 1
    return out


def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p = math.pi / 180
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def parse_attrs(text):
    out = {}
    for part in (text or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


class WalkGraph:
    """周辺セルだけを読み込んで持つグラフ。1 リクエストで使い回す。"""

    def __init__(self, db_path: Path):
        self.path = db_path
        self.available = db_path.exists()
        self.con = None
        if self.available:
            self.con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
            self.con.row_factory = sqlite3.Row
        self.nodes = {}
        self.adj = {}
        self.node_tags = {}
        self._cell_nodes = {}
        self.loaded_cells = set()
        self.has_barrier = False
        if self.con:
            self.has_barrier = bool(self.con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='barrier_edge'"
            ).fetchone())

    def load_area(self, points, radius_m=1500):
        """指定した地点の周りのセルを読む。複数地点まとめて。"""
        if not self.available:
            return
        want = set()
        for lat, lon in points:
            want.update(cells_around(lat, lon, radius_m))
        want -= self.loaded_cells
        if not want:
            return
        cur = self.con.cursor()
        chunk = list(want)
        for i in range(0, len(chunk), 400):
            part = chunk[i:i + 400]
            q = ",".join("?" * len(part))
            for r in cur.execute(
                    f"SELECT id, lat, lon, tags FROM node WHERE cell IN ({q})", part).fetchall():
                self.nodes[r["id"]] = (r["lat"], r["lon"])
                self._cell_nodes.setdefault(cell_of(r["lat"], r["lon"]), []).append(r["id"])
                if r["tags"]:
                    self.node_tags[r["id"]] = parse_attrs(r["tags"])
            # ほこナビの段差・幅員・勾配・屋根。**scripts/build_barrier_edges.py が
            # 先に貼ってある**ので、ここは辞書を引くだけ（探索中に座標で当てない）
            bar = {}
            if self.has_barrier:
                for r in cur.execute(
                        f"SELECT a, b, lev_diff, width, vtcl_slope, roof, route_type "
                        f"FROM barrier_edge WHERE cell IN ({q})", part).fetchall():
                    bar[(r["a"], r["b"])] = {
                        "lev_diff": r["lev_diff"], "width": r["width"],
                        "vtcl_slope": r["vtcl_slope"], "roof": r["roof"],
                        "route_type": r["route_type"]}
            for r in cur.execute(
                    f"SELECT a, b, length, highway, attrs, name, walk, bike, car, oneway "
                    f"FROM edge WHERE cell IN ({q})", part).fetchall():
                e = {"length": r["length"], "highway": r["highway"],
                     "attrs": parse_attrs(r["attrs"]), "name": r["name"],
                     "walk": r["walk"], "bike": r["bike"], "car": r["car"]}
                hit = bar.get((r["a"], r["b"]))
                if hit:
                    # **勾配の上り下りは向きで入れ替わる**が、いまは向きを見ずに
                    # 「きつさ」だけを使っている（登りも下りも同じ倍率）
                    e["barrier"] = hit
                self.adj.setdefault(r["a"], []).append((r["b"], e))
                if not r["oneway"]:
                    self.adj.setdefault(r["b"], []).append((r["a"], e))
                else:
                    # 一方通行でも歩行者は逆向きに通れる
                    self.adj.setdefault(r["b"], []).append(
                        (r["a"], dict(e, car=0, bike=e["bike"])))
        self.loaded_cells |= want

    def nearest_nodes(self, lat, lon, max_m=400, mode="walk", k=8):
        """近い順に k 個返す。1 つだけだと、階段でしか出入りできない地点で詰むため。"""
        cands = []
        for cell in cells_around(lat, lon, max_m):
            for nid in self._cell_nodes.get(cell, ()):
                nlat, nlon = self.nodes[nid]
                d = haversine(lat, lon, nlat, nlon)
                if d >= max_m:
                    continue
                if not any(e[mode] for _, e in self.adj.get(nid, ())):
                    continue
                cands.append((d, nid))
        cands.sort()
        return [(nid, d) for d, nid in cands[:k]]

    def nearest_node(self, lat, lon, max_m=400, mode="walk"):
        """その移動手段で通れる道に接しているノードのうち、いちばん近いもの。"""
        best, bd = None, max_m
        for cell in cells_around(lat, lon, max_m):
            for nid in self._cell_nodes.get(cell, ()):
                nlat, nlon = self.nodes[nid]
                d = haversine(lat, lon, nlat, nlon)
                if d >= bd:
                    continue
                if not any(e[mode] for _, e in self.adj.get(nid, ())):
                    continue
                best, bd = nid, d
        return best, bd

    # --- 探索 ---

    def dijkstra(self, source, profile, mode="walk", max_cost=None, targets=None):
        """source（ノードID）から各ノードへの最小コスト（秒）と直前ノードを返す。

        行き先が決まっているときは、直線距離を下限にした A* で枝を刈る（長い経路で効く）。
        """
        dist = {source: 0.0}
        prev = {}
        pq = [(0.0, 0.0, source)]
        found = set()
        targets = set(targets or [])
        speed = self._speed(mode, profile)
        goal = None
        if targets and len(targets) <= 8:
            pts = [self.nodes[t] for t in targets if t in self.nodes]
            if pts:
                goal = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

        def h(nid):
            if goal is None or nid not in self.nodes:
                return 0.0
            la, lo = self.nodes[nid]
            return haversine(la, lo, goal[0], goal[1]) / speed * 60 * 0.9

        while pq:
            _, d, u = heapq.heappop(pq)
            if d > dist.get(u, float("inf")):
                continue
            if max_cost is not None and d > max_cost:
                break
            if u in targets:
                found.add(u)
                if len(found) == len(targets):
                    break
            for v, e in self.adj.get(u, []):
                cost = profile.edge_cost(e, mode, self.node_tags.get(v, {}))
                if cost is None:
                    continue
                nd = d + cost
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = (u, e)
                    heapq.heappush(pq, (nd + h(v), nd, v))
        return dist, prev

    def route(self, origin, destination, profile, mode="walk", radius_m=None, fallback=True):
        """(lat, lon) から (lat, lon) への経路。届かなければ None。

        迂回が要る条件（ベビーカーで階段を避けるなど）では、読み込む範囲が狭いと
        道がつながらないことがあるので、失敗したら範囲を広げて数回やり直す。
        """
        if not self.available:
            return None
        straight = haversine(origin[0], origin[1], destination[0], destination[1])
        r = radius_m or max(1200, straight * 0.8 + 600)
        for attempt in range(2):
            self.load_area([origin, destination], min(r, 12000))
            # **2 回目は道への当て方を広げる。** いちばん近い節点が
            # **本線とつながっていない小島**のことがある（実測: ららぽーと柏の葉は
            # 敷地内の歩道 29 節点だけの島で、23m 先にあるのに道が出なかった。
            # 600m・24 件まで広げると 42m 先に本線の節点がある）。
            # 近い 8 件だけだと島の節点で埋まってしまい、そこから先に進めない
            snap_m, k, tries = (400, 8, 2) if attempt == 0 else (800, 32, 6)
            starts = self.nearest_nodes(*origin, max_m=snap_m, mode=mode, k=k)
            targets = self.nearest_nodes(*destination, max_m=snap_m, mode=mode, k=k)
            if starts and targets:
                tset = {nid for nid, _ in targets}
                snap = {nid: d for nid, d in targets}
                best = None
                for s0, _ in starts[:tries]:
                    dist, prev = self.dijkstra(s0, profile, mode, targets=tset)
                    for nid in tset:
                        if nid not in dist:
                            continue
                        total = dist[nid] + snap[nid] / self._speed(mode, profile) * 60
                        if best is None or total < best[0]:
                            best = (total, s0, nid, dist, prev)
                if best:
                    _, s0, t0, dist, prev = best
                    return self._build_path(s0, t0, dist, prev, origin, destination, profile, mode)
            r *= 2.0
            if r > 12000:
                break
        if fallback and mode == "walk" and profile.band.get("steps") is None:
            # 段差なしでは届かない。階段を重く見積もって道を出し、注意として伝える。
            relaxed = self.route(origin, destination, profile.relaxed(), mode,
                                 radius_m=radius_m, fallback=False)
            if relaxed:
                relaxed["step_free"] = False
                relaxed["warnings"] = [
                    f"段差を避ける道が見つかりませんでした。この道には階段が {relaxed['steps']} か所あります"]
                return relaxed
        return None

    @staticmethod
    def _speed(mode, profile):
        if mode == "bike":
            return BIKE_SPEED
        if mode == "car":
            return CAR_SPEED
        return profile.walk_speed

    def _build_path(self, s, t, dist, prev, origin, destination, profile, mode):
        nodes = [t]
        edges = []
        cur = t
        while cur != s and cur in prev:
            u, e = prev[cur]
            edges.append(e)
            nodes.append(u)
            cur = u
        nodes.reverse()
        edges.reverse()
        coords = [[origin[1], origin[0]]] + [[self.nodes[n][1], self.nodes[n][0]] for n in nodes]
        coords.append([destination[1], destination[0]])
        meters = sum(e["length"] for e in edges)
        steps = sum(1 for e in edges if e["highway"] == "steps")
        elev = sum(1 for e in edges if e["highway"] == "elevator")
        covered = sum(e["length"] for e in edges
                      if e["attrs"].get("covered") == "yes" or e["attrs"].get("tunnel"))
        bike_sidewalk = sum(e["length"] for e in edges
                            if e["highway"] == "footway"
                            and e["attrs"].get("bicycle") in ("yes", "designated"))
        return {
            "mode": mode,
            "seconds": int(dist[t]),
            "meters": int(meters),
            "steps": steps,
            "elevators": elev,
            "covered_m": int(covered),
            "bike_sidewalk_m": int(bike_sidewalk),
            "step_free": steps == 0,
            "warnings": [],
            "coords": coords,
            "obstacles": self._obstacles(nodes, edges),
        }

    def _obstacles(self, nodes, edges):
        """**その道にある「子連れだと困るもの」を、地図に出せる点にして返す。**

        出どころは 2 つ。**OpenStreetMap の `highway=steps`（階段）**と、
        **ほこナビの段差・幅員・勾配**（`barrier_edge` に貼ってある）。
        位置は辺の中点、`i` はその辺が `coords` の何番目から始まるか
        （あとで「えらんだ道と違う区間だけ」に絞るのに使う）。

        **階段は辺が細切れ**（1 か所の階段が OSM では何本もの way になる）なので、
        同じ種類が 45m 以内に続くときは 1 つの印にまとめて `count` で数える。
        """
        found = []
        for i, e in enumerate(edges):
            kinds = []
            if e["highway"] == "steps":
                kinds.append(("steps", "階段", "OpenStreetMap"))
            bar = e.get("barrier") or {}
            if bar.get("lev_diff") in LEV_DIFF_LABEL:
                kinds.append(("step", LEV_DIFF_LABEL[bar["lev_diff"]], "ほこナビ"))
            if bar.get("width") == "1":
                kinds.append(("narrow", "幅 1m 未満（ベビーカーですれ違えない）", "ほこナビ"))
            if bar.get("vtcl_slope") in ("5", "6", "7", "8"):
                kinds.append(("steep", "8% を超える坂", "ほこナビ"))
            if not kinds:
                continue
            a, b = self.nodes[nodes[i]], self.nodes[nodes[i + 1]]
            lat, lon = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            for kind, label, source in kinds:
                prev = next((o for o in reversed(found) if o["kind"] == kind), None)
                if prev and haversine(prev["lat"], prev["lon"], lat, lon) <= MERGE_M:
                    prev["count"] += 1
                    continue
                found.append({"lat": lat, "lon": lon, "kind": kind, "label": label,
                              "source": source, "count": 1, "i": i + 1})
        return found

    def reachable_points(self, origin, candidates, profile, mode="walk", max_seconds=900):
        """origin から候補地点（(id, lat, lon) の列）への所要時間。届くものだけ返す。"""
        if not self.available or not candidates:
            return {}
        pts = [origin] + [(c[1], c[2]) for c in candidates]
        self.load_area(pts, max_seconds / 60.0 * WALK_SPEED + 400)
        s, _ = self.nearest_node(*origin, mode=mode)
        if s is None:
            return {}
        node_of = {}
        for cid, lat, lon in candidates:
            nid, d = self.nearest_node(lat, lon, max_m=250, mode=mode)
            if nid is not None:
                node_of[cid] = (nid, d)
        dist, _ = self.dijkstra(s, profile, mode, max_cost=max_seconds,
                                targets={n for n, _ in node_of.values()})
        out = {}
        for cid, (nid, d) in node_of.items():
            if nid in dist:
                total = dist[nid] + d / WALK_SPEED * 60
                if total <= max_seconds:
                    out[cid] = int(total)
        return out
