"""時刻表探索（RAPTOR の簡易版）。

むなかた割引ナビからの変更点は **徒歩の脚を歩行グラフから作る**こと。
年齢で通れる道が変わると、たどり着ける停留所が変わり、**乗れる便そのものが変わる**。

- アクセス／イグレス: 探索のたびに WalkGraph で多点ダイクストラ
- 乗換: scripts/build_transfers.py が階級ごとに事前計算した表を読む（無ければ直線距離で代用）
- 駅の中: indoor.py が返す「出入口から乗り場まで」のコストを足す
"""
import math
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path

class _PlainWalk:
    """駅の中の所要時間を測るときの「ふつうに歩く人」。

    `indoor_extra()` が**同じ駅での差**を取るための基準。階段もエスカレーターも使う。
    """
    tier = "normal"
    stroller = False
    band = {"steps": 1.2}


CLUSTER_M = 60           # この距離以内の同名停留所は同じ「場所」
MIN_TRANSFER_SEC = 120
DETOUR = 1.3             # 歩行グラフが無いときの直線距離の割り増し
SHAPE_SNAP_M = 300       # 停留所が路線形状からこれ以上離れていたら、その形状は使わない
SHAPE_MAX_MPS = 42       # 切り出した線が乗車時間に対して速すぎる（≒150km/h）なら当てはめ損ね


def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p = math.pi / 180
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def snap_to_line(pts, lon, lat):
    """折れ線の**線分ごと**に「下ろした点」と「そこまでの距離 m」を出す。

    停留所は形状の頂点の上には無いので、線分に下ろす。経度は緯度で縮むので、
    比べるあいだだけ cos(lat) を掛けて平らに見る。
    """
    out = []
    k = math.cos(math.radians(lat))
    for i in range(len(pts) - 1):
        (x1, y1), (x2, y2) = pts[i], pts[i + 1]
        dx, dy = (x2 - x1) * k, y2 - y1
        if dx == 0 and dy == 0:
            t = 0.0
        else:
            t = (((lon - x1) * k) * dx + (lat - y1) * dy) / (dx * dx + dy * dy)
            t = max(0.0, min(1.0, t))
        px, py = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
        out.append((haversine(lat, lon, py, px), (px, py)))
    return out


def best_pair(da, db):
    """**乗った線分と降りた線分の組**を選ぶ（乗る方が先、という条件つきで距離の和が最小）。

    いちばん近い線分をそれぞれ選ぶ、では**周回路線で壊れる**。始発と終点が同じ
    停留所だと、乗る停留所が形状の**終わり**に当たってしまい、そこから先が無くなる
    （流山ぐりーんバスは全系統が駅に戻る周回で、実際にこれが起きた）。
    乗る方が先に来る組の中から、2 つの距離の和がいちばん小さい組を選ぶ。
    """
    best = None
    run = None                                   # ここまでで「乗る側」が最も近い線分
    for j in range(len(db)):
        if run is None or da[j][0] < da[run][0]:
            run = j
        total = da[run][0] + db[j][0]
        if best is None or total < best[0]:
            best = (total, run, j)
    return best and (best[1], best[2])


def line_length(coords):
    return sum(haversine(coords[i][1], coords[i][0], coords[i + 1][1], coords[i + 1][0])
               for i in range(len(coords) - 1))


class Network:
    def __init__(self, con, transfer_db: Path = None):
        self.con = con
        self.stops = {}
        self.place_of = {}
        self.places = {}
        self.routes = {}
        self.trips = {}
        self.trip_stops = defaultdict(list)
        self.departures = defaultdict(list)
        self.feeds = {}
        self.transfer_db = transfer_db
        self._transfers = {}
        self._shapes = {}            # 路線形状は要るときだけ読む（経路に出た便だけ）
        self._load()

    def _load(self):
        cur = self.con.cursor()
        for r in cur.execute("SELECT feed_id, feed_name, organization, license, kind "
                             "FROM feed").fetchall():
            self.feeds[r["feed_id"]] = dict(r)

        for r in cur.execute("SELECT feed_id, stop_id, stop_name, lat, lon, location_type "
                             "FROM stop").fetchall():
            if r["location_type"] not in (0, None):
                continue
            self.stops[(r["feed_id"], r["stop_id"])] = {
                "feed_id": r["feed_id"], "stop_id": r["stop_id"], "name": r["stop_name"],
                "lat": r["lat"], "lon": r["lon"]}
        self._cluster()

        for r in cur.execute("SELECT feed_id, route_id, short_name, long_name, route_type "
                             "FROM route").fetchall():
            self.routes[(r["feed_id"], r["route_id"])] = dict(r)

        for r in cur.execute("SELECT feed_id, trip_id, route_id, service_id, headsign, "
                             "shape_id, wheelchair_accessible FROM trip").fetchall():
            self.trips[(r["feed_id"], r["trip_id"])] = dict(r)

        for r in cur.execute("SELECT feed_id, trip_id, seq, stop_id, arr, dep FROM stop_time "
                             "ORDER BY feed_id, trip_id, seq").fetchall():
            place = self.place_of.get((r["feed_id"], r["stop_id"]))
            if place is None:
                continue
            self.trip_stops[(r["feed_id"], r["trip_id"])].append(
                {"seq": r["seq"], "place": place, "arr": r["arr"], "dep": r["dep"],
                 "stop": (r["feed_id"], r["stop_id"])})

        for tkey, sts in self.trip_stops.items():
            for i, st in enumerate(sts):
                if st["dep"] is not None:
                    self.departures[st["place"]].append((st["dep"], tkey, i))
        for place in self.departures:
            self.departures[place].sort(key=lambda x: x[0])

        self._load_calendar()

    def _cluster(self):
        by_name = defaultdict(list)
        for key, s in self.stops.items():
            by_name[s["name"]].append(key)
        for name, keys in by_name.items():
            clusters = []
            for key in keys:
                s = self.stops[key]
                for c in clusters:
                    if haversine(s["lat"], s["lon"], c["lat"], c["lon"]) <= CLUSTER_M:
                        c["members"].append(key)
                        break
                else:
                    clusters.append({"name": name, "lat": s["lat"], "lon": s["lon"],
                                     "members": [key]})
            for i, c in enumerate(clusters):
                pid = f"{name}#{i}" if len(clusters) > 1 else name
                c["id"] = pid
                self.places[pid] = c
                for key in c["members"]:
                    self.place_of[key] = pid

    def _load_calendar(self):
        cur = self.con.cursor()
        self.calendar = {}
        for r in cur.execute("SELECT * FROM calendar").fetchall():
            self.calendar[(r["feed_id"], r["service_id"])] = dict(r)
        self.calendar_dates = defaultdict(dict)
        for r in cur.execute("SELECT feed_id, service_id, date, exception_type "
                             "FROM calendar_date").fetchall():
            self.calendar_dates[(r["feed_id"], r["service_id"])][r["date"]] = r["exception_type"]

    WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

    def service_runs(self, feed_id, service_id, on: date):
        key = (feed_id, service_id)
        ymd = on.strftime("%Y%m%d")
        exc = self.calendar_dates.get(key, {}).get(ymd)
        if exc == 2:
            return False
        if exc == 1:
            return True
        cal = self.calendar.get(key)
        if not cal:
            return False
        if cal["start_date"] and ymd < cal["start_date"]:
            return False
        if cal["end_date"] and ymd > cal["end_date"]:
            return False
        return bool(cal[self.WEEKDAYS[on.weekday()]])

    def active_trips(self, on: date):
        return {k for k, t in self.trips.items()
                if self.service_runs(t["feed_id"], t["service_id"], on)}

    # --- 乗換表 ---

    def transfers(self, tier):
        if tier in self._transfers:
            return self._transfers[tier]
        table = defaultdict(list)
        if self.transfer_db and Path(self.transfer_db).exists():
            con = sqlite3.connect(f"file:{self.transfer_db}?mode=ro", uri=True)
            rows = con.execute("SELECT a, b, seconds FROM transfer WHERE tier=?",
                               (tier,)).fetchall()
            con.close()
            for a, b, sec in rows:
                table[a].append((b, max(sec, MIN_TRANSFER_SEC)))
        if not table:
            # 歩行グラフの表が無いときは直線距離で代用する（粗いので画面では断る）
            items = [(pid, p["lat"], p["lon"]) for pid, p in self.places.items()]
            for i, (pid, lat, lon) in enumerate(items):
                for pid2, lat2, lon2 in items[i + 1:]:
                    if abs(lat - lat2) > 0.004:
                        continue
                    d = haversine(lat, lon, lat2, lon2)
                    if d <= 300:
                        sec = max(int(d * DETOUR / 80.0 * 60), MIN_TRANSFER_SEC)
                        table[pid].append((pid2, sec))
                        table[pid2].append((pid, sec))
        self._transfers[tier] = table
        return table

    # --- 路線形状（shapes.txt） ---

    def shape_points(self, feed_id, shape_id):
        """便の路線形状。**要るときだけ DB から読んで**手元に持っておく。"""
        key = (feed_id, shape_id)
        if key not in self._shapes:
            rows = self.con.execute(
                "SELECT lon, lat FROM shape_point WHERE feed_id=? AND shape_id=? "
                "ORDER BY seq", (feed_id, shape_id)).fetchall()
            self._shapes[key] = [(r["lon"], r["lat"]) for r in rows]
        return self._shapes[key]

    def ride_shape(self, feed_id, trip_id, from_pt, to_pt, seconds=None):
        """**乗った区間だけを路線形状から切り出す。**（点は `[lon, lat]` の並び）

        停留所を直線で結ぶと、**線路や道路の曲がりが消えて地図の上で嘘になる**。
        GTFS の `shapes.txt` は便ごとの実際の形を持っているので、乗った停留所と
        降りた停留所をその線に下ろして、あいだを切り出す。

        形状を持たないフィードもある（**つくばエクスプレスは shapes.txt を出していない**）。
        そのときは `None` を返し、呼び出し側は停留所を結んだ線のままにする。
        """
        trip = self.trips.get((feed_id, trip_id)) or {}
        shape_id = (trip.get("shape_id") or "").strip()
        if not shape_id:
            return None
        pts = self.shape_points(feed_id, shape_id)
        if len(pts) < 2:
            return None
        da = snap_to_line(pts, from_pt[0], from_pt[1])
        db = snap_to_line(pts, to_pt[0], to_pt[1])
        pair = best_pair(da, db)
        if pair is None:
            return None
        i, j = pair
        if max(da[i][0], db[j][0]) > SHAPE_SNAP_M:
            return None                       # 形状から遠い＝当てはめ損ねている
        coords = [list(da[i][1])] + [list(p) for p in pts[i + 1:j + 1]] + [list(db[j][1])]
        out = [coords[0]]
        for c in coords[1:]:
            if c != out[-1]:                  # 同じ点が続くと線が汚れる
                out.append(c)
        if len(out) < 2:
            return None
        # **遠回りそのものは疑わない。** コミュニティバスは 1 停留所ぶんでも
        # 大きく回ることがあり、それが実際の経路だから（直線距離との比では弾けない）。
        # 当てはめ損ねを見るのは**かかる時間**: 乗車時間に対して長すぎる線はおかしい。
        length = line_length(out)
        if seconds and seconds > 0 and length / seconds > SHAPE_MAX_MPS:
            return None
        return out

    def nearby_places(self, lat, lon, limit_m=1000, top=30):
        out = []
        for pid, p in self.places.items():
            d = haversine(lat, lon, p["lat"], p["lon"])
            if d <= limit_m:
                out.append((pid, d))
        out.sort(key=lambda x: x[1])
        return out[:top]


class Router:
    def __init__(self, network: Network, walk_graph=None, indoor=None):
        self.net = network
        self.walk = walk_graph
        self.indoor = indoor
        self._indoor_extra = {}

    # --- 駅の中（GTFS-Pathways）---

    def indoor_extra(self, profile):
        """**駅の中でよけいにかかる秒数。**（2026-09-28 追加）

        GTFS-Pathways は「出入口から乗り場まで」を持っている。港区の大江戸線 12 駅では
        徒歩で 261〜703 秒、**ベビーカーだと 422〜952 秒**（新宿はそもそも段差なしで
        乗り場に行けない）。

        **絶対の秒数を足してはいけない。** Pathways があるのは 12 駅だけで、
        JR や東京メトロの駅には無い。実際の所要時間を持つ駅にだけ数分足すと、
        **中身が分かっている駅ほど不利になる**という逆さまのことが起きる。

        そこで足すのは**同じ駅の「徒歩」との差**だけにした。
          - ベビーカーで大門なら 519 − 414 = **105 秒**
          - 麻布十番なら 952 − 703 = **249 秒**
        Pathways の無い駅は差が測れないので 0（情報が無いことを不利に扱わない）。
        **データを持たない駅と比べて公平**で、かつ「ベビーカーだと駅の中で余計にかかる」
        という、この作品が言いたい事実だけがコストに乗る。

        段差なしでは乗り場に行けない駅（新宿）は、**通れないことにはしない**
        （大江戸線の管理範囲しかデータが無く、実際には他社のエレベーターで
        つながっていることがある）。徒歩と同じだけ余計にかかる、と控えめに見積もる。
        """
        key = (profile.tier, profile.stroller, profile.band.get("steps") is None)
        if key in self._indoor_extra:
            return self._indoor_extra[key]
        out = {}
        if self.indoor and self.indoor.available:
            base_profile = _PlainWalk()
            for sid, station in self.indoor.stations.items():
                base = self.indoor.station_seconds(sid, base_profile)
                if not base:
                    continue
                mine = self.indoor.station_seconds(sid, profile)
                extra = base if mine is None else max(0, mine - base)
                if extra <= 0:
                    continue
                for pid in self._places_for(station):
                    out[pid] = max(out.get(pid, 0), int(extra))
        self._indoor_extra[key] = out
        return out

    def _places_for(self, station):
        """Pathways の駅を、探索が使う「場所」に対応づける。

        名前が同じで 400m 以内のものを拾う（同じ駅名でも 60m のまとまりで
        `大門#0` `大門#1` のように分かれることがある）。

        **同じフィードのものに限る**のが肝心。新宿は 400m 以内に
        `新宿#0`（都営地下鉄）と `新宿#2` `新宿#3`（JR東日本）があり、
        名前と距離だけで拾うと**大江戸線の構内の時間を JR 新宿駅にも付けて**しまう。
        Pathways が言っているのは都営地下鉄の構内のことだけ。
        """
        feed = station.get("feed_id_of_route") or self.indoor.route_feed_id
        got = []
        for pid, p in self.net.places.items():
            if p["name"] != station.get("stop_name"):
                continue
            if not any(f == feed for f, _ in p.get("members", [])):
                continue
            if haversine(p["lat"], p["lon"], station["lat"], station["lon"]) <= 400:
                got.append(pid)
        return got

    # --- 徒歩の脚 ---

    def access(self, point, profile, limit_m=1500, max_seconds=None):
        """地点 → 近くの場所（停留所・駅）への徒歩時間。歩行グラフを使う。"""
        net = self.net
        point = (point[0], point[1])
        max_seconds = max_seconds or profile.max_walk_seconds
        near = net.nearby_places(point[0], point[1], limit_m=limit_m)
        if not near:
            return {}
        extra = self.indoor_extra(profile)
        if self.walk and self.walk.available:
            cands = [(pid, net.places[pid]["lat"], net.places[pid]["lon"]) for pid, _ in near]
            got = self.walk.reachable_points(point, cands, profile, "walk", max_seconds)
            if got:
                return {pid: sec + extra.get(pid, 0) for pid, sec in got.items()}
        # 歩行グラフが無い／届かないときは直線距離で代用
        out = {}
        for pid, d in near:
            sec = int(d * DETOUR / profile.walk_speed * 60)
            if sec <= max_seconds:
                out[pid] = sec + extra.get(pid, 0)
        return out

    def reachable_places(self, origin, when, profile, budget_seconds,
                         on_date=None, max_transfers=2):
        """**到達圏のための探索。** 出発時刻から budget 秒以内に着ける場所と、その到着時刻。

        行き先を決めずに広げるだけなので、経路の組み立て（_rebuild）はしない。
        """
        net = self.net
        on = on_date or date.today()
        active = net.active_trips(on)
        transfers = net.transfers(profile.tier)
        limit = when + budget_seconds

        best = {}
        arrivals = [dict()]
        starts = self.access(origin, profile, limit_m=1500,
                             max_seconds=min(budget_seconds, profile.max_walk_seconds))
        for pid, sec in starts.items():
            t = when + sec
            if t <= limit and (pid not in best or t < best[pid]):
                best[pid] = t
                arrivals[0][pid] = t

        for rnd in range(1, max_transfers + 2):
            current = {}
            for pid, t_arrive in arrivals[rnd - 1].items():
                if t_arrive > limit:
                    continue
                self._ride(pid, t_arrive, rnd, active, profile, best, current, {})
                for pid2, sec in transfers.get(pid, []):
                    t = t_arrive + sec
                    if t > limit or (pid2 in best and best[pid2] <= t):
                        continue
                    best[pid2] = t
                    current[pid2] = t
            arrivals.append({p: t for p, t in current.items() if t <= limit})
        return {p: t for p, t in best.items() if t <= limit}

    def search(self, origin, destination, when, profile, max_transfers=2, on_date=None,
               access=None, egress=None):
        net = self.net
        on = on_date or date.today()
        active = net.active_trips(on)
        transfers = net.transfers(profile.tier)

        best = {}
        arrivals = [dict()]
        from_leg = {}

        starts = access if access is not None else self.access(origin, profile)
        if not starts:
            return []
        for pid, sec in starts.items():
            t = when + sec
            if pid not in best or t < best[pid]:
                best[pid] = t
                arrivals[0][pid] = t
                from_leg[(0, pid)] = self._walk_leg(origin[2], net.places[pid],
                                                    when, t, origin, None)

        for rnd in range(1, max_transfers + 2):
            prev = arrivals[rnd - 1]
            current = {}
            for pid, t_arrive in prev.items():
                self._ride(pid, t_arrive, rnd, active, profile, best, current, from_leg)
                for pid2, sec in transfers.get(pid, []):
                    t = t_arrive + sec
                    if pid2 in best and best[pid2] <= t:
                        continue
                    best[pid2] = t
                    current[pid2] = t
                    from_leg[(rnd, pid2)] = self._walk_leg(
                        net.places[pid]["name"], net.places[pid2], t_arrive, t,
                        (net.places[pid]["lat"], net.places[pid]["lon"]), (rnd - 1, pid))
            arrivals.append(current)

        goals = egress if egress is not None else self.access(destination, profile)
        results = []
        for rnd in range(len(arrivals)):
            for pid, sec in goals.items():
                if pid not in arrivals[rnd]:
                    continue
                t = arrivals[rnd][pid] + sec
                legs = self._rebuild(from_leg, rnd, pid)
                if not legs:
                    continue
                if legs[0]["mode"] == "walk" and legs[0].get("seconds", 0) < 60:
                    legs = legs[1:]
                if not legs:
                    continue
                if sec > 60:
                    p = net.places[pid]
                    legs.append({"mode": "walk", "from": p["name"], "to": destination[2],
                                 "dep": arrivals[rnd][pid], "arr": t, "seconds": sec,
                                 "from_lat": p["lat"], "from_lon": p["lon"],
                                 "to_lat": destination[0], "to_lon": destination[1]})
                results.append({"arrive": t, "legs": legs})
        return self._dedupe(results)

    def _ride(self, pid, t_arrive, rnd, active, profile, best, current, from_leg):
        net = self.net
        boarded = set()
        for dep, tkey, idx in net.departures.get(pid, []):
            if tkey not in active or dep is None or dep < t_arrive:
                continue
            if dep > t_arrive + 3 * 3600:
                break
            trip = net.trips[tkey]
            rkey = (trip["feed_id"], trip["route_id"])
            if rkey in boarded:
                continue
            # 明示的に「車椅子不可」の便だけ外す。空欄は不明であって不可ではない。
            if profile.stroller and (trip.get("wheelchair_accessible") or "") == "2":
                continue
            boarded.add(rkey)
            sts = net.trip_stops[tkey]
            route = net.routes[rkey]
            for j in range(idx + 1, len(sts)):
                st = sts[j]
                arr = st["arr"] if st["arr"] is not None else st["dep"]
                if arr is None:
                    continue
                target = st["place"]
                if target in best and best[target] <= arr:
                    continue
                best[target] = arr
                current[target] = arr
                from_leg[(rnd, target)] = {
                    "mode": "transit", "feed_id": trip["feed_id"], "trip_id": trip["trip_id"],
                    "route_name": route["long_name"] or route["short_name"],
                    "route_short": route["short_name"], "route_type": route["route_type"],
                    "operator": net.feeds.get(trip["feed_id"], {}).get("organization"),
                    "feed_name": net.feeds.get(trip["feed_id"], {}).get("feed_name"),
                    "from": net.places[sts[idx]["place"]]["name"],
                    "to": net.places[target]["name"],
                    "from_place": sts[idx]["place"], "to_place": target,
                    "from_lat": net.places[sts[idx]["place"]]["lat"],
                    "from_lon": net.places[sts[idx]["place"]]["lon"],
                    "to_lat": net.places[target]["lat"], "to_lon": net.places[target]["lon"],
                    "dep": dep, "arr": arr,
                    "coords": [[net.places[sts[k]["place"]]["lon"],
                                net.places[sts[k]["place"]]["lat"]]
                               for k in range(idx, j + 1)],
                    "stops_passed": j - idx,
                    "wheelchair": (trip.get("wheelchair_accessible") or ""),
                    "prev": (rnd - 1, pid),
                }

    @staticmethod
    def _walk_leg(from_name, place, dep, arr, from_point, prev):
        return {"mode": "walk", "from": from_name, "to": place["name"],
                "dep": dep, "arr": arr, "seconds": arr - dep,
                "from_lat": from_point[0], "from_lon": from_point[1],
                "to_lat": place["lat"], "to_lon": place["lon"], "prev": prev}

    @staticmethod
    def _rebuild(from_leg, rnd, pid):
        legs = []
        cursor = (rnd, pid)
        seen = set()
        while cursor in from_leg:
            if cursor in seen:
                return []
            seen.add(cursor)
            leg = from_leg[cursor]
            legs.append({k: v for k, v in leg.items() if k != "prev"})
            cursor = leg.get("prev")
            if cursor is None:
                break
        legs.reverse()
        return legs

    @staticmethod
    def _dedupe(results):
        best = {}
        for r in results:
            sig = tuple((l.get("route_name"), l.get("trip_id"))
                        for l in r["legs"] if l["mode"] == "transit")
            if not sig:
                continue
            if sig not in best or r["arrive"] < best[sig]["arrive"]:
                best[sig] = r
        out = list(best.values())
        out.sort(key=lambda r: (r["arrive"], len(r["legs"])))
        return out

    def search_many(self, origin, destination, when, profile, max_transfers=2, on_date=None,
                    window_sec=3 * 3600, limit=5):
        """出発時刻をずらして候補を集める。徒歩の脚は 1 回だけ計算して使い回す。"""
        net = self.net
        on = on_date or date.today()
        active = net.active_trips(on)
        access = self.access(origin, profile)
        egress = self.access(destination, profile)
        if not access or not egress:
            return []

        times = {when}
        for pid, sec in access.items():
            for dep, tkey, _ in net.departures.get(pid, []):
                if tkey not in active or dep is None:
                    continue
                start = dep - sec
                if when <= start <= when + window_sec:
                    times.add(start)
        candidates = []
        for t in sorted(times)[:12]:
            candidates.extend(self.search(origin, destination, t, profile,
                                          max_transfers=max_transfers, on_date=on,
                                          access=access, egress=egress))
        merged = {}
        for r in candidates:
            sig = tuple((l.get("route_name"), l.get("trip_id"))
                        for l in r["legs"] if l["mode"] == "transit")
            if not sig:
                continue
            cur = merged.get(sig)
            key = (r["arrive"], len(r["legs"]), -min(l["dep"] for l in r["legs"]))
            if cur is None or key < (cur["arrive"], len(cur["legs"]),
                                     -min(l["dep"] for l in cur["legs"])):
                merged[sig] = r
        out = list(merged.values())
        for r in out:
            r["depart"] = min(l["dep"] for l in r["legs"])
            r["duration"] = r["arrive"] - r["depart"]
            r["transfers"] = max(0, sum(1 for l in r["legs"] if l["mode"] == "transit") - 1)
        if not out:
            return []
        best_duration = min(r["duration"] for r in out)
        best_arrive = min(r["arrive"] for r in out)
        out = [r for r in out
               if r["duration"] <= best_duration * 2.5 + 900 and r["arrive"] <= best_arrive + 5400]
        out.sort(key=lambda r: (r["arrive"], r["duration"]))
        return out[:limit]
