"""年齢プロファイル。月齢から「どう歩くか」を決める。

このモジュールがこの作品の芯で、年齢を動かすと
  ・通れない道が変わる（ベビーカーは階段を通れない）
  ・同じ道でも感じる長さが変わる（勾配・未舗装・車通り）
  ・歩ける上限時間が変わる
ようにしている。
"""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = REPO_ROOT / "data" / "profiles.yaml"

BUSY_ROADS = {"primary", "primary_link", "secondary", "secondary_link", "trunk", "trunk_link",
              "motorway", "motorway_link"}
QUIET_ROADS = {"living_street", "residential", "pedestrian", "footway", "path", "track"}
CAR_SPEED_BY_HIGHWAY = {       # m/分
    "motorway": 1300, "motorway_link": 800, "trunk": 800, "trunk_link": 600,
    "primary": 550, "primary_link": 450, "secondary": 500, "secondary_link": 400,
    "tertiary": 420, "residential": 280, "living_street": 180, "service": 180,
    "unclassified": 350, "busway": 300, "road": 300,
}


def _conf():
    return yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))


def load_bands():
    return _conf()["bands"]


def load_modes():
    """移動手段（徒歩／自転車／ベビーカー／徒歩＋公共交通）。"""
    return _conf()["modes"]


def load_reach_modes():
    """**到達圏として地図に同時に描く手段。** 既定では徒歩・自転車・徒歩＋公共交通の 3 つ。

    ユーザーに 1 つ選ばせるのではなく、この 3 つを重ねて出す
    （「どの手段ならどこまで行けるか」を並べて見せたいため）。
    """
    return [m for m in load_modes() if m.get("reach")]


def load_modes_public():
    """画面に渡す分だけ。重みの数字までは出さない。"""
    return [{"id": m["id"], "label": m["label"], "transit": bool(m.get("transit")),
             "reach": bool(m.get("reach")),
             "graph_mode": m.get("graph_mode", "walk"),
             "walk_speed": m.get("walk_speed"),
             "steps_blocked": m.get("steps") is None}
            for m in load_modes()]


class Profile:
    """ある月齢に対する移動の仕方。"""

    def __init__(self, band: dict, months=None, stroller: bool = None):
        self.band = band
        self.months = months
        self.id = band["id"]
        self.label = band["label"]
        self.tier = band.get("tier", "normal")
        self.walk_speed = float(band.get("walk_speed", 80))
        self.max_walk_seconds = int(band.get("max_walk_seconds", 900))
        self.break_minutes = int(band.get("break_minutes", 60))
        # ベビーカーは既定を上書きできる（抱っこ紐で行く、など）
        self.stroller = band.get("stroller", False) if stroller is None else bool(stroller)
        self.transit = bool(band.get("transit"))
        # 歩行グラフをどの手段で通るか。自転車は通れる道と速さが変わる
        self.graph_mode = band.get("graph_mode", "walk")

    @property
    def graph_speed(self):
        """`graph_mode` で進む速さ（m/分）。到達圏で読む範囲の見積もりに使う。"""
        if self.graph_mode == "bike":
            from backend.walk import BIKE_SPEED
            return BIKE_SPEED
        return self.walk_speed

    # --- 道 1 本あたりのコスト（秒）。None なら通れない ---

    def edge_cost(self, edge, mode="walk", to_node_tags=None):
        b = self.band
        highway = edge["highway"]
        attrs = edge["attrs"]

        if mode == "car":
            if not edge["car"]:
                return None
            speed = CAR_SPEED_BY_HIGHWAY.get(highway, 300)
            return edge["length"] / speed * 60

        if mode == "bike":
            if not edge["bike"]:
                return None
            weight = 1.0
            if highway == "cycleway":
                weight *= float(b.get("cycleway", 1.0))
            elif highway == "footway":
                if attrs.get("bicycle") in ("yes", "designated"):
                    weight *= float(b.get("bike_sidewalk", 1.0))
                else:
                    weight *= 1.6          # 押して歩く前提
            elif highway in BUSY_ROADS:
                weight *= float(b.get("busy_road", 1.3))
            if attrs.get("surface") in ("gravel", "ground", "dirt", "sand", "unpaved"):
                weight *= float(b.get("unpaved", 1.2))
            from backend.walk import BIKE_SPEED
            return edge["length"] / BIKE_SPEED * 60 * weight

        # 徒歩
        if not edge["walk"]:
            return None
        weight = 1.0
        if highway == "steps":
            w = b.get("steps")
            if w is None:
                return None                # ベビーカーは通れない
            weight *= float(w)
            if attrs.get("handrail") != "yes" and b.get("steps_no_handrail"):
                weight *= float(b["steps_no_handrail"]) / float(w)
            if self.stroller and not (attrs.get("ramp") or attrs.get("ramp:stroller")):
                return None
        elif highway == "elevator":
            weight *= float(b.get("elevator", 1.0))
        elif highway in BUSY_ROADS and attrs.get("sidewalk") in (None, "no", "none"):
            weight *= float(b.get("busy_road", 1.3))

        if attrs.get("incline") not in (None, "", "0%"):
            weight *= float(b.get("incline", 1.3))
        if attrs.get("surface") in ("gravel", "ground", "dirt", "sand", "unpaved"):
            weight *= float(b.get("unpaved", 1.2))
        width = attrs.get("width")
        if width and b.get("narrow"):
            try:
                if float(width.replace("m", "").strip()) < 1.5:
                    weight *= float(b["narrow"])
            except ValueError:
                pass
        if attrs.get("covered") == "yes" or attrs.get("tunnel") == "building_passage":
            weight *= float(b.get("covered", 1.0))

        # --- ほこナビの段差・幅員・勾配・屋根（2026-09-28 追加）---
        #
        # **OSM に無い事実がここで初めて効く。** OSM の縁石タグは流山市 38・新宿区 150 しか
        # なく、段差は実質わからなかった。ほこナビは **1 区間ごとに段差・幅員・勾配・屋根**を
        # 持っている（港区 2,542 区間）。`scripts/build_barrier_edges.py` が歩行グラフの
        # 辺に先に貼ってあるので、ここでは辞書を見るだけ。
        #
        # **通行不可にはしない。** 当てはめは「エッジの中点から 25m 以内のいちばん近い区間」
        # なので、歩道 1 本ぶんずれて付くことがある（ほこナビが「階段」と言う 236 本のうち、
        # OSM も `steps` と言っているのは 110 本だけ）。誤って道を塞ぐと経路そのものが
        # 出なくなるので、**重いコストにして「あるなら避ける」**に留めてある。
        # 階段を本当に塞ぐのは OSM の `highway=steps`（上のほう）の仕事。
        bar = edge.get("barrier")
        if bar:
            hk = b.get("hokonavi") or {}
            lev = bar.get("lev_diff")
            if lev == "5":                      # 10cm 超
                weight *= float(hk.get("step_over_10cm", 1.0))
            elif lev == "4":                    # 5〜10cm
                weight *= float(hk.get("step_over_5cm", 1.0))
            elif lev == "3":                    # 2〜5cm
                weight *= float(hk.get("step_over_2cm", 1.0))
            if bar.get("width") == "1":         # 有効幅員 1m 未満（ベビーカーですれ違えない）
                weight *= float(hk.get("narrow", 1.0))
            slope = bar.get("vtcl_slope")
            if slope in ("5", "6", "7", "8"):   # 8% を超える
                weight *= float(hk.get("steep", 1.0))
            elif slope in ("3", "4"):           # 5〜8%
                weight *= float(hk.get("slope", 1.0))
            if bar.get("roof") == "2":          # 屋根あり（1 は「なし」）
                weight *= float(hk.get("roof", 1.0))

        # 段差（ノード側のタグ）
        tags = to_node_tags or {}
        kerb = tags.get("kerb")
        if kerb in ("raised", "yes") and self.stroller:
            weight *= 1.8
        if tags.get("barrier") in ("gate", "bollard", "kissing_gate", "stile") and self.stroller:
            weight *= 1.3

        return edge["length"] / self.walk_speed * 60 * weight

    # --- スポットの絞り込み ---

    def spot_matches(self, spot):
        if self.months is None:      # 移動手段で選んでいるときは年齢で絞らない
            return True
        lo = spot.get("age_min")
        hi = spot.get("age_max")
        if lo is not None and self.months < lo:
            return False
        if hi is not None and self.months > hi:
            return False
        return True

    def relaxed(self):
        """段差なしでは届かないときの逃げ道。階段を重いコストで通れるようにする。

        「行けません」で終わらせず、「階段が N か所あります」と伝えるために使う。
        """
        band = dict(self.band)
        band["steps"] = 6.0
        p = Profile(band, self.months, stroller=False)
        p.label = self.label
        p.tier = self.tier
        p.relaxed_from = self.id
        return p

    def plain(self):
        """**当て馬。** 段差も坂もよけず、いちばん短い道を選ぶだけのふつうの歩き方。

        よけた道を 1 本だけ出しても、**何をよけたのかが画面に出ない**。
        同じ区間を「よけない道」でも引いて並べると、遠回りの代わりに
        何を通らずに済んだのかが地図の上で見える。

        重みを全部 1.0 に落とし、階段も通れるようにする。**歩く速さは変えない**
        （速さまで変えると「よけた道が遠い」のか「歩くのが遅い」のか分からなくなる）。
        `id` を分けてあるのは、経路のキャッシュで元の profile と混ざらないようにするため。
        """
        band = dict(self.band)
        band.update({"id": f"{self.band['id']}-plain", "steps": 1.0, "steps_no_handrail": None,
                     "elevator": 1.0, "incline": 1.0, "unpaved": 1.0, "busy_road": 1.0,
                     "covered": 1.0, "narrow": 1.0, "hokonavi": {}})
        p = Profile(band, self.months, stroller=False)
        p.label = "段差をよけない最短"
        p.tier = self.tier
        p.plain_from = self.id
        return p

    def to_json(self):
        return {"id": self.id, "label": self.label, "months": self.months,
                "transit": self.transit, "graph_mode": self.graph_mode,
                "tier": self.tier, "stroller": self.stroller,
                "walk_speed": self.walk_speed, "max_walk_seconds": self.max_walk_seconds,
                "break_minutes": self.break_minutes,
                "steps_blocked": self.band.get("steps") is None}


def for_mode(mode_id: str, stroller=None) -> Profile:
    """移動手段（walk / bike / stroller / transit）のプロファイル。年齢は見ない。"""
    modes = load_modes()
    for mode in modes:
        if mode["id"] == mode_id:
            return Profile(mode, None, stroller)
    return Profile(modes[0], None, stroller)


def for_months(months: int, stroller=None) -> Profile:
    months = int(months)
    bands = load_bands()
    for band in bands:
        lo, hi = band["months"]
        if lo <= months <= hi:
            return Profile(band, months, stroller)
    return Profile(bands[-1] if months > 0 else bands[0], months, stroller)
