"""線路の形（OpenStreetMap）から、乗った区間を切り出す。

**なぜ GTFS だけで足りないか**: 便ごとの形は `shapes.txt` に入れられることになっていて、
流山ぐりーんバスは出しているが、**つくばエクスプレスの GTFS には shapes.txt が無い**。
駅と駅を直線で結ぶと、地図の上で線路の曲がりが消えてしまう。

`scripts/build_rail_shapes.py` が作った `data/rail/<stage>.geojson`（路線ごとの線）を読み、
乗った駅と降りた駅をその線に下ろして、あいだを切り出す。**時刻表の代わりではなく、
地図に描く線だけ**の話なので、所要時間や運賃には一切かかわらない。

出典は OpenStreetMap contributors（ODbL）。
"""
import json

from backend.stages import REPO_ROOT
from backend.transit import haversine, line_length, snap_to_line

RAIL_DIR = REPO_ROOT / "data" / "rail"

SNAP_M = 400          # 駅が線からこれ以上離れていたら、その線は使わない
MAX_DETOUR = 2.5      # 直線距離に対してこれ以上遠回りなら、当てはめ損ねとみなす


class RailShapes:
    def __init__(self, stage):
        self.path = RAIL_DIR / f"{stage.id}.geojson"
        self.lines = []
        self._load()

    def _load(self):
        if not self.path.exists():
            return                          # 線路の形を作っていない舞台（作らなくても動く）
        data = json.loads(self.path.read_text(encoding="utf-8"))
        for f in data.get("features", []):
            coords = f.get("geometry", {}).get("coordinates") or []
            if len(coords) >= 2:
                self.lines.append({"name": f["properties"].get("name", ""),
                                   "operator": f["properties"].get("operator", ""),
                                   "coords": [(c[0], c[1]) for c in coords]})

    @property
    def available(self):
        return bool(self.lines)

    def names(self):
        return sorted({l["name"] for l in self.lines})

    def between(self, from_pt, to_pt, name=None, operator=None):
        """乗った駅 → 降りた駅の線を返す（`[lon, lat]` の並び）。無ければ None。

        絞り方は **路線名 → 運行者 → 全部** の順。名前は時刻表と OSM で揃わないことがある
        （GTFS の「東武アーバンパークライン」は OSM では「東武野田線」）。そのときは
        **運行者で絞る**と、同じ会社の線だけが残って当てやすい。それも合わなければ全部から
        選ぶ（乗り降りの駅がどちらも近い線が、その路線である公算が高い）。
        """
        by_name = [l for l in self.lines if name and l["name"] == name]
        by_op = [l for l in self.lines if operator and l["operator"] == operator]
        cands = by_name or by_op or self.lines
        best = None
        for line in cands:
            pts = line["coords"]
            da = snap_to_line(pts, from_pt[0], from_pt[1])
            db = snap_to_line(pts, to_pt[0], to_pt[1])
            i = min(range(len(da)), key=lambda k: da[k][0])
            j = min(range(len(db)), key=lambda k: db[k][0])
            far = max(da[i][0], db[j][0])
            if far > SNAP_M:
                continue
            if best is None or far < best[0]:
                best = (far, pts, i, da[i][1], j, db[j][1])
        if best is None:
            return None
        _, pts, i, pa, j, pb = best
        if i == j:
            out = [list(pa), list(pb)]
        elif i < j:
            out = [list(pa)] + [list(p) for p in pts[i + 1:j + 1]] + [list(pb)]
        else:
            # 線の向きは路線の向きと関係ない（OSM の way の向きのまま）ので、
            # 降りる駅が先に来ていたら切ってから**ひっくり返す**
            out = [list(pb)] + [list(p) for p in pts[j + 1:i + 1]] + [list(pa)]
            out.reverse()
        dedup = [out[0]]
        for c in out[1:]:
            if c != dedup[-1]:
                dedup.append(c)
        if len(dedup) < 2:
            return None
        straight = haversine(from_pt[1], from_pt[0], to_pt[1], to_pt[0])
        if straight > 100 and line_length(dedup) > MAX_DETOUR * straight:
            return None                     # 遠回りしすぎ＝当てはめ損ねている
        return dedup
