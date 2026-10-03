"""モード横断の経路づくり。

トータルナビ／電車／バス／徒歩／自転車／車を並べて返す。
- 公共交通は transit.Router（徒歩の脚は歩行グラフ）
- 徒歩・自転車・車は walk.WalkGraph
- 歩いた区間には barrier.BarrierLayer で段差の裏づけを足す
- 駅では indoor.IndoorNetwork で「段差なしで入れる出入口」を添える
"""
from datetime import date

from backend import railshape

RAIL_TYPES = {0, 1, 2, 5, 7, 12}
BUS_TYPES = {3, 11, 700, 715}


def diverge(chosen, plain, eps=1e-9):
    """2 本の線が**分かれてから合流するまで**の範囲を、`plain` 側の添字で返す。

    同じ道なら `None`。頭とお尻の重なっているところを外すのは**見た目のため**で、
    同じ道を 2 本重ねて描くと、どちらが対象の経路か分からなくなる。
    座標は同じノードの表から来るので、そのまま突き合わせられる。
    """
    def same(u, v):
        return abs(u[0] - v[0]) < eps and abs(u[1] - v[1]) < eps

    n = min(len(chosen), len(plain))
    p = 0
    while p < n and same(chosen[p], plain[p]):
        p += 1
    if p >= len(plain):
        return None
    s = 0
    while s < n - p and same(chosen[len(chosen) - 1 - s], plain[len(plain) - 1 - s]):
        s += 1
    q = len(plain) - s
    return (p, q) if q > p else None


def fmt(sec):
    sec = int(sec) % (24 * 3600) if sec >= 24 * 3600 else int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}"


class Planner:
    def __init__(self, stage, router, walk_graph, barrier=None, indoor=None):
        self.stage = stage
        self.router = router
        self.walk = walk_graph
        self.barrier = barrier
        self.indoor = indoor
        self._station_index = None
        self._rail = None
        self._walk_shapes = {}

    # --- 駅の中 ---

    def station_for(self, name, lat, lon):
        """場所の名前・座標から、構内データのある駅を探す。"""
        if not (self.indoor and self.indoor.available):
            return None
        if self._station_index is None:
            self._station_index = [
                (sid, s["stop_name"], s["lat"], s["lon"])
                for sid, s in self.indoor.stations.items()]
        base = (name or "").replace("駅", "")
        for sid, sname, slat, slon in self._station_index:
            if sname == base or sname.replace("駅", "") == base:
                return sid
        return None

    def entrance_note(self, leg, profile):
        sid = self.station_for(leg.get("from"), leg.get("from_lat"), leg.get("from_lon"))
        if not sid:
            return None
        free = self.indoor.step_free_entrances(sid)
        total = len(self.indoor.entrances.get(sid, []))
        if free:
            names = "・".join(e["name"] for e in free[:3])
            return {"station": self.indoor.stations[sid]["stop_name"],
                    "step_free": True, "entrances": total, "step_free_count": len(free),
                    "text": f"{names} の出入口から入ると、階段を使わずにホームまで行けます",
                    "list": free}
        return {"station": self.indoor.stations[sid]["stop_name"],
                "step_free": False, "entrances": total, "step_free_count": 0,
                "text": "公開されている構内データでは、階段を使わない行き方が確認できませんでした。"
                        "駅係員や他社のエレベーターが使えることがあります",
                "list": []}

    # --- 各モード ---

    def transit_plans(self, origin, destination, when, profile, on_date=None, limit=5):
        results = self.router.search_many(origin, destination, when, profile,
                                          on_date=on_date or date.today(), limit=limit)
        used = profile
        if not results and profile.band.get("steps") is None:
            # 段差なしの条件で見つからないときは、歩ける上限を少し伸ばして探し直す
            stretched = profile.relaxed()
            stretched.max_walk_seconds = int(profile.max_walk_seconds * 1.6)
            results = self.router.search_many(origin, destination, when, stretched,
                                              on_date=on_date or date.today(), limit=limit)
            for r in results:
                r.setdefault("warnings", []).append(
                    "段差を避けたまま乗れる便が見つからず、条件をゆるめて探しています")
            if results:
                # **探索に使った条件でそのまま飾る。** 徒歩の脚の線をここで引き直すので、
                # 時間を出したときと違う条件で引くと、線と時間が食い違う
                used = stretched
        for r in results:
            self.decorate(r, used)
        for r in self.shown(results):
            self.shape_walk_legs(r, used)
        return results

    @property
    def rail(self):
        """線路の形（OSM）。**使うときに初めて読む**（107KB の GeoJSON）。"""
        if self._rail is None:
            self._rail = railshape.RailShapes(self.stage)
        return self._rail

    def ride_shape(self, leg):
        """**乗った区間の線を、実際の路線の形に引き直す。**

        停留所を直線で結ぶと、地図の上で線路やバス路線の曲がりが消える。出どころは 2 つ:

        1. **GTFS の `shapes.txt`**（流山ぐりーんバスが出している）。便ごとの形なので、
           これがあるときはこれがいちばん正しい
        2. **OpenStreetMap の線路**（`data/rail/<stage>.geojson`）。
           **つくばエクスプレスの GTFS には shapes.txt が無い**ので、鉄道はこちらで補う

        どちらも無ければ停留所を結んだ線のまま（`transit._ride` が入れている）。
        どこから来た線かは `leg["shape"]`（`gtfs` / `osm` / `stops`）に残す。

        **探索の中ではなくここで作る**のは、画面に出す経路の脚だけで済むため
        （探索中は何千本も候補が出る）。
        """
        from_pt = (leg["from_lon"], leg["from_lat"])
        to_pt = (leg["to_lon"], leg["to_lat"])
        coords = self.router.net.ride_shape(leg["feed_id"], leg["trip_id"], from_pt, to_pt,
                                            seconds=leg["arr"] - leg["dep"])
        source = "gtfs"
        if not coords and leg.get("route_type") in RAIL_TYPES:
            coords = self.rail.between(from_pt, to_pt, name=leg.get("route_name"),
                                       operator=leg.get("operator"))
            source = "osm"
        leg["shape"] = source if coords else "stops"
        if coords:
            leg["coords"] = coords
        return leg

    WALK_SHAPE_MAX = 2048

    def _route_cached(self, a, b, profile):
        """同じ区間・同じ条件の歩きは 1 回だけ引く（見つからなかったことも覚える）。"""
        key = (profile.id, profile.stroller, profile.graph_mode, profile.band.get("steps"),
               round(a[0], 6), round(a[1], 6), round(b[0], 6), round(b[1], 6))
        if key not in self._walk_shapes:
            if len(self._walk_shapes) >= self.WALK_SHAPE_MAX:
                self._walk_shapes.clear()
            self._walk_shapes[key] = self.walk.route(a, b, profile, "walk", fallback=False)
        return self._walk_shapes[key]

    def walk_shape(self, leg, profile):
        """**歩いた区間の線を、実際の道なりに引き直す。**

        探索（`transit.Router`）が返す徒歩の脚は、始点と終点の 2 点しか持っていない。
        そのまま地図に描くと、**建物や川を突き抜けた直線**が出発地から駅まで引かれる。
        所要時間のほうはすでに歩行グラフ（`walk.reachable_points`）で測ってあるので、
        ここでは**時間には触れず、線と距離だけ**を同じグラフの道なりに引き直す。

        引くときの条件は**探索が使ったものと同じ**（ベビーカーで階段を避けるなら、
        線も階段を避けた道になる）。`walk.route` の `fallback` は使わない。
        階段を許した道を描いてしまうと、**時間の根拠になった道と違う線**が出るため。
        引けなかったときは 2 点のままにして、`leg["shape"]` に `straight` と残す。

        乗る区間（`ride_shape`）と違って、**出発地側・目的地側の脚は候補をまたいで
        同じものが何度も出る**（5 本の候補が同じ駅まで歩く）。覚えておくと、
        `scripts/build_static.py` で行き先の数だけ経路を作るときの探索が減る。
        """
        leg["shape"] = "straight"
        if not (self.walk and self.walk.available):
            return leg
        path = self._route_cached((leg["from_lat"], leg["from_lon"]),
                                  (leg["to_lat"], leg["to_lon"]), profile)
        if not path:
            return leg
        leg["coords"] = path["coords"]
        leg["meters"] = path["meters"]
        leg["steps"] = path["steps"]
        leg["elevators"] = path["elevators"]
        leg["step_free"] = path["step_free"]
        leg["shape"] = "osm"
        return leg

    def compare_walk(self, leg, profile):
        """**当て馬の道**（段差をよけない、ふつうの最短）を脚に添える。

        よけた道を 1 本だけ出しても、**何をよけたのかは画面に出ない**。「3 分よけいに
        かかる遠回りの道」に見えてしまう。同じ区間を**よけない道でも引いて並べ**、
        そちらに階段や段差の印を置くと、遠回りの代わりに何を通らずに済んだのかが
        地図の上で分かる。

        **出すのはベビーカーのときだけ。** ふつうの歩きでは、よけているものが
        ほとんど無く、線が 2 本になるぶん地図が混むだけになる。

        次の 3 つのどれかに当てはまったら**出さない**（出しても何も言えないため）。

        1. よけない道にも困るものが無い
        2. よけない道が、えらんだ道とまったく同じ
        3. 違うのは頭とお尻だけで、**分かれている区間に困るものが無い**

        線は**分かれてから合流するまで**だけを渡す（同じ道を 2 本重ねない）。
        """
        if not (profile.stroller and self.walk and self.walk.available):
            return leg
        coords = leg.get("coords")
        if not coords or len(coords) < 2:
            return leg
        path = self._route_cached((leg["from_lat"], leg["from_lon"]),
                                  (leg["to_lat"], leg["to_lon"]), profile.plain())
        if not path or not path.get("obstacles"):
            return leg
        span = diverge(coords, path["coords"])
        if not span:
            return leg
        lo, hi = span
        marks = [o for o in path["obstacles"] if o["i"] + 1 >= lo and o["i"] < hi]
        if not marks:
            return leg
        extra = max(0, int(leg.get("meters") or 0) - path["meters"])
        leg["compare"] = {
            # 前後 1 点ずつ伸ばして、えらんだ道から分かれる形に見せる
            "coords": path["coords"][max(0, lo - 1):min(len(path["coords"]), hi + 1)],
            "meters": path["meters"],
            "extra_m": extra,
            "extra_seconds": int(extra / profile.walk_speed * 60),
            "obstacles": marks,
        }
        return leg

    def decorate(self, result, profile):
        rail = any(l.get("route_type") in RAIL_TYPES for l in result["legs"]
                   if l["mode"] == "transit")
        bus = any(l.get("route_type") in BUS_TYPES for l in result["legs"]
                  if l["mode"] == "transit")
        result["kinds"] = [k for k, v in (("train", rail), ("bus", bus)) if v]
        result["depart_text"] = fmt(result.get("depart", 0))
        result["arrive_text"] = fmt(result["arrive"])
        for leg in result["legs"]:
            leg["dep_text"] = fmt(leg["dep"])
            leg["arr_text"] = fmt(leg["arr"])
            if leg["mode"] == "transit":
                self.ride_shape(leg)
                # 明示的に 1（可）のときだけ言う。空欄は「不明」であって「不可」ではない
                if (leg.get("wheelchair") or "") == "1":
                    leg["stroller_note"] = "ベビーカーのまま乗れます"
                note = self.entrance_note(leg, profile)
                if note:
                    leg["entrance"] = note
        return result

    @staticmethod
    def pick_kind(results, kind):
        """その手段だけで行ける経路の先頭。画面の「電車」「バス」のカードになる。"""
        return next((r for r in results if r["kinds"] == [kind]), None)

    @classmethod
    def shown(cls, results):
        """**画面がカードにする経路だけ**を返す（トータルナビ・電車・バス）。

        探索は 5 本まで返すが、`frontend/app.js` の `PLAN_ROWS` が地図に描くのは
        この 3 枚で、残りは画面に出ない。徒歩の線を引き直すのは 1 脚ごとに
        歩行グラフを歩く分だけ高くつくので、**出るものにだけ**かける。
        3 枚が同じ経路を指すことはよくあるので、重なりは取り除く。
        """
        picks = [results[0] if results else None,
                 cls.pick_kind(results, "train"), cls.pick_kind(results, "bus")]
        out = []
        for r in picks:
            if r is not None and not any(r is o for o in out):
                out.append(r)
        return out

    def shape_walk_legs(self, result, profile):
        """経路の徒歩の脚に、道なりの線と、当て馬の道を入れる。"""
        for leg in result["legs"]:
            if leg["mode"] == "walk":
                self.walk_shape(leg, profile)
                self.compare_walk(leg, profile)
        return result

    # **さがす範囲の上限（半径 6km）より短くしてはいけない。**
    # ここが 5km だったせいで、**半径 5〜6km の行き先に徒歩がまったく出なかった**
    # （2026-09-30・ユーザー指摘「徒歩で到達できない場所は基本的には存在しないはず」）。
    # 5km 台の行き先は流山市で 14 件・港区で 5 件あり、**公共交通も無い行き先では
    # 「経路は見つかりませんでした」と出ていた**（歩けば 1 時間強で着く）。
    # スライダの上限（`build_static.RADII` の最大 6,000m）と必ずそろえること。
    # 直線ではなく道なりで測るので、少し余裕をみて 6.5km にしてある。
    MAX_STRAIGHT_M = {"walk": 6500, "bike": 15000, "car": 30000}

    def walk_plan(self, origin, destination, when, profile, mode="walk"):
        from backend.walk import haversine
        straight = haversine(origin[0], origin[1], destination[0], destination[1])
        if straight > self.MAX_STRAIGHT_M.get(mode, 20000):
            return {"unavailable": True, "mode": mode,
                    "reason": {"walk": "歩くには遠すぎます", "bike": "自転車には遠すぎます",
                               "car": "遠すぎます"}[mode],
                    "straight_m": int(straight)}
        path = self.walk.route((origin[0], origin[1]), (destination[0], destination[1]),
                               profile, mode)
        if not path:
            return None
        if self.barrier:
            self.barrier.annotate(path, profile)
        leg = dict(path)
        # 道にある印は**当て馬の道でしか使わない**。えらんだ道のぶんは持ち回らない
        leg.pop("obstacles", None)
        leg.update({"mode": mode, "from": origin[2], "to": destination[2],
                    "dep": when, "arr": when + path["seconds"],
                    "dep_text": fmt(when), "arr_text": fmt(when + path["seconds"]),
                    "from_lat": origin[0], "from_lon": origin[1],
                    "to_lat": destination[0], "to_lon": destination[1]})
        if mode == "walk":
            self.compare_walk(leg, profile)
        return {"arrive": when + path["seconds"], "depart": when,
                "duration": path["seconds"], "transfers": 0, "legs": [leg],
                "kinds": [mode], "depart_text": fmt(when),
                "arrive_text": fmt(when + path["seconds"]),
                "warnings": path.get("warnings", []), "notes": path.get("notes", [])}

    # 画面に出す手段。**自転車と車は外した**（2026-09-30・ユーザー指示）。
    # 理由は 2 つ: 公共交通ではないことと、**推奨されているオープンデータを
    # 1 つも使っていない**こと（自前の歩行・道路グラフと OSM だけで出せてしまう）。
    # `walk_plan("bike"/"car")` は残してあるので、`modes` に入れれば今までどおり返る。
    PLAN_MODES = ("walk",)

    def plan_all(self, origin, destination, when, profile, on_date=None, modes=None):
        """画面に出す並び。無いものは「なし」を返す（黙って消さない）。"""
        out = {}
        transit = self.transit_plans(origin, destination, when, profile, on_date)
        out["total"] = transit[0] if transit else None
        out["transit"] = transit
        out["train"] = self.pick_kind(transit, "train")
        out["bus"] = self.pick_kind(transit, "bus")
        for mode in (modes if modes is not None else self.PLAN_MODES):
            out[mode] = self.walk_plan(origin, destination, when, profile, mode)
        out["total"] = self.pick_total(out)
        return out

    @staticmethod
    def pick_total(out):
        """トータルナビは公共交通と徒歩のうち早いほう。

        自転車・車は「持っている人だけの手段」なので、ここには混ぜず別枠で出す。
        """
        best = None
        for key in ("total", "walk"):
            cand = out.get(key)
            if not cand or cand.get("unavailable"):
                continue
            if best is None or cand["arrive"] < best["arrive"]:
                best = cand
        return best
