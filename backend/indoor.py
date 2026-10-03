"""駅の中（GTFS-Pathways と構内地図）。

大江戸線 12 駅ぶんのデータで、**どの出入口から入れば段差なしでホームに行けるか**を出す。
実測（2026-09-22）: 出入口 67 か所のうち、階段・エスカレータを使わずにホームへ行けるのは 15 か所。

stop_times が参照するのは location_type=0 の「乗り場」で、
  出入口(2) → 通路(3) → 乗降場所(4) → 乗り場(0) → 駅(1)
という親子関係になっている。だから「どの出入口から入るか」を乗る便のコストに直接乗せられる。

**表示は肯定形にする。**「この駅は行けない」とは書かない（大江戸線の管理範囲しか
データが無く、実際には隣の地下街や他社のエレベーターでつながっていることがあるため）。
"""
from collections import defaultdict, deque

MODE_WALK, MODE_STAIRS, MODE_ESCALATOR, MODE_ELEVATOR = 1, 2, 4, 5
MODE_LABEL = {1: "通路", 2: "階段", 3: "動く歩道", 4: "エスカレーター", 5: "エレベーター", 6: "改札",
              7: "乗換通路"}
STAIR_PENALTY_SEC = 45          # 階段 1 か所ぶんの上り下り
ELEVATOR_SEC = 70               # 待ち時間込み
ESCALATOR_SPEED = 50.0          # m/分
BASE_SPEED = 70.0               # 構内は人が多いので外より少し遅く見る


class IndoorNetwork:
    # Pathways は時刻表とは別のフィードで配られる（`toei_pathways`）。
    # 経路探索の駅と対応づけるときは、**時刻表のほうのフィード**を見る必要がある
    ROUTE_FEED_OF = {"toei_pathways": "toei_train"}

    def __init__(self, con, feed_id=None):
        self.route_feed_id = self.ROUTE_FEED_OF.get(feed_id, feed_id)
        self.available = False
        self.stops = {}
        self.adj = defaultdict(list)
        self.stations = {}
        self.entrances = defaultdict(list)
        self.boarding = defaultdict(list)      # 駅 -> [乗り場(location_type=0)]
        self.area_of = {}                      # 乗降場所(4) -> 乗り場(0)
        if con is None:
            return
        cur = con.cursor()
        rows = cur.execute(
            "SELECT feed_id, stop_id, stop_name, lat, lon, location_type, parent_station, level_id "
            "FROM pathway_stop" + (" WHERE feed_id=?" if feed_id else ""),
            (feed_id,) if feed_id else ()).fetchall()
        if not rows:
            return
        for r in rows:
            self.stops[r["stop_id"]] = dict(r)
        for r in cur.execute(
                "SELECT from_stop, to_stop, mode, bidirectional, length FROM pathway"
                + (" WHERE feed_id=?" if feed_id else ""),
                (feed_id,) if feed_id else ()).fetchall():
            self.adj[r["from_stop"]].append((r["to_stop"], r["mode"], r["length"] or 0))
            if r["bidirectional"]:
                self.adj[r["to_stop"]].append((r["from_stop"], r["mode"], r["length"] or 0))
        self.available = True
        self._index()

    def _root(self, stop_id):
        s = self.stops.get(stop_id)
        while s and s.get("parent_station"):
            s = self.stops.get(s["parent_station"])
        return s["stop_id"] if s else stop_id

    def _index(self):
        for sid, s in self.stops.items():
            if s["location_type"] == 1:
                self.stations[sid] = s
        for sid, s in self.stops.items():
            root = self._root(sid)
            if s["location_type"] == 2:
                self.entrances[root].append(sid)
            elif s["location_type"] == 0 and s.get("parent_station"):
                self.boarding[root].append(sid)
            elif s["location_type"] == 4 and s.get("parent_station"):
                self.area_of[sid] = s["parent_station"]

    # --- 探索 ---

    def _cost(self, mode, length, profile):
        if mode == MODE_ELEVATOR:
            return ELEVATOR_SEC
        if mode == MODE_ESCALATOR:
            return length / ESCALATOR_SPEED * 60 + 15
        if mode == MODE_STAIRS:
            return length / BASE_SPEED * 60 + STAIR_PENALTY_SEC
        return length / BASE_SPEED * 60 + 5

    def _blocked(self, mode, profile):
        """このプロファイルで通れない構内設備か。"""
        if profile is None:
            return False
        if profile.band.get("steps") is None and mode in (MODE_STAIRS, MODE_ESCALATOR):
            return True          # ベビーカーは階段もエスカレーターも使わない
        return False

    def reach(self, source, profile):
        """出入口から各地点への所要時間（秒）。"""
        dist = {source: 0.0}
        q = deque([source])
        # 辺のコストが一様でないので簡単なラベル修正法で回す（規模が小さいので十分）
        while q:
            u = q.popleft()
            for v, mode, length in self.adj.get(u, ()):
                if self._blocked(mode, profile):
                    continue
                nd = dist[u] + self._cost(mode, length, profile)
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    q.append(v)
        return dist

    def station_entrances(self, station_id, profile=None):
        """出入口ごとに、段差なしで乗り場まで行けるかと所要時間。"""
        out = []
        boards = self.boarding.get(station_id, [])
        for eid in self.entrances.get(station_id, []):
            dist = self.reach(eid, profile)
            reached = {}
            for area, board in self.area_of.items():
                if board in boards and area in dist:
                    reached[board] = min(reached.get(board, 1e9), dist[area])
            e = self.stops[eid]
            out.append({
                "id": eid,
                "name": e.get("stop_name") or eid,
                "lat": e.get("lat"), "lon": e.get("lon"),
                "reaches": {b: int(s) for b, s in reached.items()},
                "seconds": int(min(reached.values())) if reached else None,
                "step_free": bool(reached),
            })
        out.sort(key=lambda x: (x["seconds"] is None, x["seconds"] or 0))
        return out

    def station_seconds(self, station_id, profile=None):
        """**出入口から乗り場まで、いちばん早くて何秒かかるか。**（2026-09-28 追加）

        経路探索のコストに足すために使う。ベビーカー（`steps: null`）では
        階段とエスカレーターが通れないので、**段差なしで入れる出入口だけ**が対象になり、
        その駅を使うこと自体が高くつく（あるいは使えない）と分かる。

        戻り値は秒。どの出入口からも乗り場に行けなければ `None`。
        """
        got = [e["seconds"] for e in self.station_entrances(station_id, profile)
               if e["seconds"] is not None]
        return min(got) if got else None

    def step_free_entrances(self, station_id):
        """階段・エスカレータを使わずに乗り場へ行ける出入口（プロファイル非依存の素の事実）。"""
        class _P:
            band = {"steps": None}
        return [e for e in self.station_entrances(station_id, _P()) if e["step_free"]]

    # --- 外のグラフとつなぐ ---

    def entrance_points(self, station_id):
        return [(e["id"], self.stops[e["id"]]["lat"], self.stops[e["id"]]["lon"])
                for e in ({"id": eid} for eid in self.entrances.get(station_id, []))]

    def summary(self):
        if not self.available:
            return {"available": False, "stations": 0, "entrances": 0, "step_free": 0}
        total = sum(len(v) for v in self.entrances.values())
        free = sum(len(self.step_free_entrances(sid)) for sid in self.stations)
        return {"available": True, "stations": len(self.stations),
                "entrances": total, "step_free": free}
