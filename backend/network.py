"""**経路探索が使っている公共交通そのもの**を、地図に出せる形で返す。

画面に出るのは「さがした 1 本の経路」だけで、**その裏でどれだけの路線と停留所を見ているか**
は見えなかった。港区なら 4 フィード・200 路線・のべ 8 万便、流山市なら 4 フィード・44 路線。
それを重ねて出し、チェックボックスで消せるようにする（2026-09-30・ユーザー指示）。

**出すのは探索が実際に使っているものだけ。** 飾りで路線図を描くのではない。

- **停留所・駅**は `transit.Network.places`（＝探索の単位）。同じ名前で 150m 以内のものを
  1 つにまとめた「乗り場」で、`location_type` が 0 以外（駅舎・出入口）は入っていない。
  **つまり、画面に出る点の数がそのまま探索の頂点の数**になる
- **路線の線**は route ごとに 1 本。**その route でいちばん停留所の多い便**の形を使う
  （行きと帰りで経路が違うことがあるので、多いほうを代表にする）

線の出どころは経路の線と同じ 3 段（`backend/plan.py` の `ride_shape` と同じ考え方）。

1. **GTFS の `shapes.txt`** … 流山ぐりーんバス・都営バスが出している
2. **OpenStreetMap の線路**（`data/rail/<stage>.geojson`）… 鉄道の GTFS は
   4 フィードとも `shapes.txt` を出していないので、鉄道はこれで補う
3. **停留所を結んだ線** … どちらも無いとき。**そのことを画面に出す**（`shape` の欄）
"""
from backend import railshape

BUS_TYPE = 3
# GTFS の route_type。3（バス）以外の在来の値はすべて「電車」に寄せる
# （0 路面電車・1 地下鉄・2 鉄道。都営の GTFS は 3 つとも入っている）
RAIL_TYPES = (0, 1, 2, 5, 7, 11, 12)


def kind_of(route_type):
    return "bus" if route_type == BUS_TYPE else "train"


class TransitOverlay:
    """舞台ごとに 1 回作って使い回す（`StageContext` が持つ）。"""

    def __init__(self, stage, net):
        self.stage = stage
        self.net = net
        self._built = None
        self._reps = None

    def geojson(self):
        if self._built is None:
            self._built = self._build()
        return self._built

    # --- 中身 ---

    def _representative_trips(self):
        """route ごとに、**いちばん停留所の多い便**を 1 つ選ぶ。

        便は港区で 9 万本あり、走査に 15 秒ほどかかるので 1 回だけにする。
        """
        if self._reps is not None:
            return self._reps
        best = {}
        for tkey, stops in self.net.trip_stops.items():
            trip = self.net.trips.get(tkey)
            if not trip or len(stops) < 2:
                continue
            rkey = (trip["feed_id"], trip["route_id"])
            if rkey not in best or len(stops) > len(best[rkey][1]):
                best[rkey] = (tkey, stops)
        self._reps = best
        return best

    def _stop_line(self, stops):
        """停留所を順に結んだ線。同じ点が続くところは落とす。"""
        out = []
        for st in stops:
            s = self.net.stops.get(st["stop"])
            if not s:
                continue
            pt = [s["lon"], s["lat"]]
            if not out or out[-1] != pt:
                out.append(pt)
        return out

    def _line_for(self, rkey, tkey, stops, rail):
        """route 1 本ぶんの線と、その出どころ。"""
        feed_id, trip_id = tkey
        route = self.net.routes.get(rkey) or {}
        shape_id = (self.net.trips.get(tkey, {}).get("shape_id") or "").strip()
        if shape_id:
            pts = self.net.shape_points(feed_id, shape_id)
            if len(pts) >= 2:
                return [[p[0], p[1]] for p in pts], "gtfs"
        line = self._stop_line(stops)
        if len(line) < 2:
            return None, None
        if route.get("route_type") in RAIL_TYPES and rail.available:
            feed = self.net.feeds.get(feed_id) or {}
            osm, hit = self._rail_line(line, rail, route, feed)
            # **駅どうしを 1 区間ずつ線路に当てる**（路線まるごとでは当たらない）。
            # `data/rail/<stage>.geojson` は舞台の区域＋余白しか持っていないので、
            # 遠くの区間は当たらない。**半分以上当たったときだけ「線路から」と名乗る**
            if hit and hit * 2 >= len(line) - 1:
                return osm, "osm"
            if hit:
                return osm, "mixed"
        return line, "stops"

    def _rail_line(self, line, rail, route, feed):
        """駅を結んだ線を、**1 区間ずつ**線路の形に置き換える。当たった区間数も返す。"""
        name = route.get("long_name") or route.get("short_name")
        operator = feed.get("organization")
        out, hit = [line[0]], 0
        for a, b in zip(line, line[1:]):
            seg = rail.between(tuple(a), tuple(b), name=name, operator=operator)
            if seg and len(seg) > 2:
                hit += 1
                out += [list(p) for p in seg[1:]]
            else:
                out.append(b)
        return out, hit

    def _build(self):
        rail = railshape.RailShapes(self.stage)
        lines, counts = [], {}
        for rkey, (tkey, stops) in sorted(self._representative_trips().items()):
            feed_id, route_id = rkey
            route = self.net.routes.get(rkey) or {}
            feed = self.net.feeds.get(feed_id) or {}
            coords, source = self._line_for(rkey, tkey, stops, rail)
            if not coords:
                continue
            kind = kind_of(route.get("route_type"))
            counts.setdefault(feed_id, {"routes": 0, "by_shape": {}})
            counts[feed_id]["routes"] += 1
            counts[feed_id]["by_shape"][source] = \
                counts[feed_id]["by_shape"].get(source, 0) + 1
            lines.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {
                    "kind": kind, "feed": feed_id,
                    "feed_name": feed.get("feed_name") or feed_id,
                    "route": (route.get("short_name") or route.get("long_name") or route_id),
                    "route_long": route.get("long_name") or "",
                    "shape": source, "stops": len(stops),
                },
            })

        # **探索の単位そのもの**を点にする。1 つの乗り場に複数のフィードが来る
        # （田町駅は JR と都営バス）ので、来ているものを全部持たせる
        kinds_at, feeds_at = {}, {}
        for rkey, (tkey, stops) in self._representative_trips().items():
            route = self.net.routes.get(rkey) or {}
            kind = kind_of(route.get("route_type"))
            for st in stops:
                pid = st["place"]
                kinds_at.setdefault(pid, set()).add(kind)
                feeds_at.setdefault(pid, set()).add(rkey[0])
        stops_fc = []
        for pid, place in sorted(self.net.places.items()):
            kinds = kinds_at.get(pid)
            if not kinds:
                continue                    # どの便も来ない乗り場は出さない
            names = sorted(self.net.feeds.get(f, {}).get("feed_name") or f
                           for f in feeds_at.get(pid, ()))
            stops_fc.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [place["lon"], place["lat"]]},
                "properties": {
                    # 両方来る乗り場は電車として描く（そのほうが大きい印になる）
                    "kind": "train" if "train" in kinds else "bus",
                    "name": place["name"], "feeds": "・".join(names),
                    "platforms": len(place["members"]),
                },
            })

        feeds = []
        for feed_id, feed in sorted(self.net.feeds.items()):
            c = counts.get(feed_id)
            if not c:
                continue
            kinds = {f["properties"]["kind"] for f in lines
                     if f["properties"]["feed"] == feed_id}
            feeds.append({
                "id": feed_id, "name": feed.get("feed_name") or feed_id,
                "organization": feed.get("organization"),
                "kind": "train" if "train" in kinds else "bus",
                "routes": c["routes"],
                "stops": sum(1 for f in stops_fc
                             if feed.get("feed_name", feed_id) in f["properties"]["feeds"]),
                # 線をどこから引いたか。**「停留所を結んだだけ」も隠さずに出す**
                "by_shape": c["by_shape"],
            })
        return {
            "lines": {"type": "FeatureCollection", "features": lines},
            "stops": {"type": "FeatureCollection", "features": stops_fc},
            "feeds": feeds,
            "counts": {
                "train": sum(1 for f in lines if f["properties"]["kind"] == "train"),
                "bus": sum(1 for f in lines if f["properties"]["kind"] == "bus"),
                "train_stops": sum(1 for f in stops_fc
                                   if f["properties"]["kind"] == "train"),
                "bus_stops": sum(1 for f in stops_fc if f["properties"]["kind"] == "bus"),
            },
            # 線路の形を持っていない舞台では、鉄道の線が駅を結んだ直線になる
            "rail_osm": rail.available,
        }
