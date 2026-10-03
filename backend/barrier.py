"""ほこナビの屋外歩行空間ネットワークを、OSM の歩行グラフに重ねる。

ほこナビには段差（lev_diff）・幅員（width）・縦断勾配（vtcl_slope）・屋根（roof）・
エレベーター（elevator）が入っているが、OSM にはほとんど無い（実測: 縁石タグは
流山市 38・東京 23 区でも 1 区あたり 150 前後）。ID 体系が違うので**座標で対応づける**。

**いまの舞台（流山市）にほこナビのデータは無い**ので、経路に
「このあたりは段差のデータがありません」を出す。ほこナビを持つ舞台を足せばそのまま重なる。
"""
import csv
import math
from pathlib import Path

CELL = 0.002              # 約 200m 角で索引する
MATCH_M = 25              # この距離以内のほこナビ区間を「同じ道」とみなす

# 区分コード。**歩行空間ネットワークデータ整備仕様（2024年7月・国土交通省）**の表 3.2 に合わせてある。
# https://www.mlit.go.jp/sogoseisaku/soukou/content/001757259.pdf
#
# **2026-09-28 に仕様書を読み直して 2 か所直した。**
#   - `WIDTH` の区切りが間違っていた（2 は「1〜1.5m」ではなく **1.0〜2.0m 未満**、
#     3 は 2.0〜3.0m 未満、4 は **3.0m 以上**）
#   - `ROOF` は **1 が「なし」・2 が「あり」**。`annotate()` が 1 を「屋根あり」と
#     数えていたので、逆に読んでいた（港区の 2,336 区間を「屋根あり」と誤って数えていた）
#   - `LEV_DIFF` に 4（5〜10cm）と 5（**10cm超**）が抜けていた。
#     港区には **10cm 超の段差が 367 区間**あり、そこが素通しになっていた
LEV_DIFF = {"1": "段差なし", "2": "2cm以下", "3": "2〜5cm", "4": "5〜10cm",
            "5": "10cm超", "99": "不明"}
WIDTH = {"1": "1m未満", "2": "1〜2m", "3": "2〜3m", "4": "3m以上", "99": "不明"}
VTCL_SLOPE = {"1": "平坦", "2": "5%以下", "3": "5〜8%の上り", "4": "5〜8%の下り",
              "5": "8〜18%の上り", "6": "8〜18%の下り", "7": "18%超の上り",
              "8": "18%超の下り", "99": "不明"}
ROUTE_TYPE = {"1": "", "2": "動く歩道", "3": "踏切", "4": "エレベーター",
              "5": "エスカレーター", "6": "階段", "7": "スロープ", "99": "不明"}
ROOF = {"1": "なし", "2": "あり", "99": "不明"}
# エレベーター: 1 なし／2 あり（バリアフリー対応なし）／3 車椅子対応／4 視覚障害者対応／
# 5 車椅子・視覚障害者対応／99 不明
ELEVATOR_NONE = ("1", "", "99")

# **段差が大きい順**。ベビーカーで越えられないものを決めるのに使う
STEP_OVER_5CM = ("4", "5")
STEP_OVER_2CM = ("3", "4", "5")
SLOPE_STEEP = ("5", "6", "7", "8")      # 8% を超える
SLOPE_MILD = ("3", "4")                 # 5〜8%


def _mid(a, b):
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


def _cell(lat, lon):
    return (int(math.floor(lat / CELL)), int(math.floor(lon / CELL)))


def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p = math.pi / 180
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


class BarrierLayer:
    """ほこナビの link を中点で索引したもの。"""

    def __init__(self, stage):
        self.stage = stage
        self.index = {}
        self.count = 0
        self.total_km = 0.0
        self.datasets = []
        self._load(stage)

    @property
    def available(self):
        return self.count > 0

    def _load(self, stage):
        for layer in stage.walk_layers:
            if layer.get("kind") != "hokonavi":
                continue
            for name in layer.get("datasets", []):
                base = stage.raw_dir / "hokonavi" / name
                node_path, link_path = base / "node.csv", base / "link.csv"
                if not (node_path.exists() and link_path.exists()):
                    continue
                nodes = {}
                for r in self._read(node_path):
                    if r.get("lat") and r.get("lon"):
                        nodes[r["node_id"]] = (float(r["lat"]), float(r["lon"]))
                n = 0
                for r in self._read(link_path):
                    a, b = nodes.get(r.get("start_id")), nodes.get(r.get("end_id"))
                    if not a or not b:
                        continue
                    info = {
                        "lev_diff": (r.get("lev_diff") or "").strip(),
                        "width": (r.get("width") or "").strip(),
                        "vtcl_slope": (r.get("vtcl_slope") or "").strip(),
                        "roof": (r.get("roof") or "").strip(),
                        "elevator": (r.get("elevator") or "").strip(),
                        "brail_tile": (r.get("brail_tile") or "").strip(),
                        "route_type": (r.get("route_type") or "").strip(),
                        "a": a, "b": b,
                    }
                    mid = _mid(a, b)
                    self.index.setdefault(_cell(*mid), []).append((mid, info))
                    try:
                        self.total_km += float(r.get("distance") or 0) / 1000
                    except ValueError:
                        pass
                    n += 1
                self.count += n
                self.datasets.append({"name": name, "links": n})

    @staticmethod
    def _read(path: Path):
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp932"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            return []
        return list(csv.DictReader(text.splitlines()))

    def lookup(self, lat, lon):
        """その地点にいちばん近いほこナビ区間の属性。無ければ None。"""
        if not self.available:
            return None
        best, bd = None, MATCH_M
        cy, cx = _cell(lat, lon)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for mid, info in self.index.get((cy + dy, cx + dx), ()):
                    d = haversine(lat, lon, mid[0], mid[1])
                    if d < bd:
                        best, bd = info, d
        return best

    def annotate(self, path, profile):
        """経路（walk.route の返り値）に、段差の裏づけを足す。

        表示は肯定形にする。「ここは通れない」ではなく「段差なしが確認できた区間」を数える。
        """
        if not path:
            return path
        coords = path.get("coords") or []
        checked = 0
        step_free = 0
        roofed = 0
        unknown = 0
        details = []
        for a, b in zip(coords, coords[1:]):
            mid = ((a[1] + b[1]) / 2, (a[0] + b[0]) / 2)
            info = self.lookup(mid[0], mid[1]) if self.available else None
            if not info:
                unknown += 1
                continue
            checked += 1
            if info["lev_diff"] in ("1", "2"):
                step_free += 1
            elif info["lev_diff"] in STEP_OVER_2CM:
                details.append(f"{LEV_DIFF[info['lev_diff']]}の段差が確認されています")
            if info["roof"] == "2":          # 1 は「屋根なし」。2 が「あり」
                roofed += 1
        path["barrier"] = {
            "source": "ほこナビ" if self.available else None,
            "checked": checked,
            "step_free": step_free,
            "roofed": roofed,
            "unknown": unknown,
            "notes": sorted(set(details)),
        }
        if not self.available:
            path.setdefault("warnings", []).append(
                "このあたりは歩道の段差データがありません（OSM の情報だけで案内しています）")
        elif checked and step_free == checked:
            path.setdefault("notes", []).append(
                f"歩道{checked}区間のうち{step_free}区間で「段差なし」が確認できました")
        return path

    def coverage(self):
        return {"available": self.available, "links": self.count,
                "km": round(self.total_km, 1), "datasets": self.datasets}
