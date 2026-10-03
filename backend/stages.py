"""舞台（対象の市区町村）の設定を読む。

舞台ごとに違うものは data/stages/*.yaml に閉じ込め、コードには市区町村名を書かない。
舞台によって「駅の中（GTFS-Pathways）」や「ほこナビの屋外歩行空間」を持つものと
持たないものがある。**持たない舞台でも画面が壊れないこと**がこのモジュールの責任。

`enabled: false` の舞台は**まだ使えない舞台**。画面にボタンだけ出して押せなくするための
見出しで、データもコードも持たない。既定では load_all() が返さないので、データ整備の
スクリプトは触らずに済む。
"""
import json
import math
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGE_DIR = REPO_ROOT / "data" / "stages"
BOUNDARY_DIR = REPO_ROOT / "data" / "boundary"
RAW = REPO_ROOT / "data" / "raw"


class Stage:
    def __init__(self, conf: dict):
        self.conf = conf
        self.id = conf["id"]
        self.name = conf["name"]
        self.pref = conf.get("pref", "")
        # まだ使えない舞台（enabled: false）は見出ししか持たないので、
        # 区域コードから下は無くても読めるようにしておく
        self.enabled = conf.get("enabled", True)
        self.note = conf.get("note") or ""
        self.code = conf.get("code")
        self.pref_code = conf.get("pref_code")
        self.center = conf.get("center") or {}
        self.home_radius_km = conf.get("home_radius_km", 10)
        # 自宅は候補から選ぶ（地図を触らなくても使える）
        self.homes = conf.get("homes") or []
        self.transit = conf.get("transit") or []
        self.walk_layers = conf.get("walk_layers") or [{"kind": "osm"}]
        self.indoor = conf.get("indoor")
        self.spots = conf.get("spots") or []
        # **舞台の外の行き先。** 自前整備の CSV（`kind: local`）と、
        # 区市町村をまたいで配られているデータセット（`kind: hokonavi_toilet` など）の 2 通り。
        # どちらも scripts/build_outside_spots.py が読む
        self.outside_sources = conf.get("outside") or []
        # **画面に出さない種類。** データは取り込んだまま残し、行き先として出すのをやめる
        # （2026-09-30・ユーザー指示「データは一応残して、サービスから消す」）。
        # 消すのではなく隠すので、`data/spots/generated/*.json` も取り込みのスクリプトも
        # そのままでよく、この 1 行を消せば元に戻る。`backend/spots.py` が読み込みで落とし、
        # 落とした件数は「この舞台で使えるデータ」に理由つきで出す
        self.hide_types = conf.get("hide_types") or []
        # 市ホームページの巡回先（概要と公式ページの独自整備に使う）。
        # **どの自治体のどこを見るかは舞台ごとに違う**ので、コードでなく設定に置く
        self.web_pages = conf.get("web_pages") or {}
        # 天気予報（気象庁）の区域コード。舞台ごとに違うので設定に置く
        self.jma_area = conf.get("jma_area") or "130000"
        self._boundary = None

    # --- 区域 ---

    @property
    def boundary_path(self) -> Path:
        return BOUNDARY_DIR / f"{self.id}.geojson"

    def boundary(self):
        """区域の GeoJSON（FeatureCollection）。無ければ None。"""
        if self._boundary is None and self.boundary_path.exists():
            self._boundary = json.loads(self.boundary_path.read_text(encoding="utf-8"))
        return self._boundary

    def rings(self):
        geo = self.boundary()
        if not geo:
            return []
        out = []
        for f in geo["features"]:
            g = f["geometry"]
            polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
            for poly in polys:
                out.append(poly[0])
        return out

    def contains(self, lat, lon) -> bool:
        """自宅がこの舞台の中か。区域データが無ければ True（登録を止めない）。"""
        rings = self.rings()
        if not rings:
            return True
        for ring in rings:
            inside = False
            n = len(ring)
            for i in range(n):
                x1, y1 = ring[i][0], ring[i][1]
                x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
                if (y1 > lat) != (y2 > lat):
                    xin = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
                    if lon < xin:
                        inside = not inside
            if inside:
                return True
        return False

    def bbox(self):
        rings = self.rings()
        if not rings:
            c = self.center
            return [c["lon"] - 0.1, c["lat"] - 0.1, c["lon"] + 0.1, c["lat"] + 0.1]
        xs = [p[0] for r in rings for p in r]
        ys = [p[1] for r in rings for p in r]
        return [min(xs), min(ys), max(xs), max(ys)]

    def area_km2(self):
        total = 0.0
        for ring in self.rings():
            s = 0.0
            for i in range(len(ring)):
                x1, y1 = ring[i][0], ring[i][1]
                x2, y2 = ring[(i + 1) % len(ring)][0], ring[(i + 1) % len(ring)][1]
                s += x1 * y2 - x2 * y1
            total += abs(s) / 2
        lat = self.center.get("lat", 35.7)
        return total * 111.0 * 111.0 * math.cos(math.radians(lat))

    # --- パス ---

    @property
    def db_path(self) -> Path:
        return REPO_ROOT / "data" / f"{self.id}.sqlite"

    @property
    def raw_dir(self) -> Path:
        return RAW / self.id

    # --- 画面に出す「この舞台で使えるデータ」 ---

    def coverage(self):
        has_hokonavi = any(l.get("kind") == "hokonavi" for l in self.walk_layers)
        return {
            "stage": self.id,
            "name": self.name,
            "enabled": True,
            "indoor": bool(self.indoor),
            "hokonavi": has_hokonavi,
            "transit": [
                {"id": t["id"], "name": t.get("feed_name", t["id"]),
                 "organization": t.get("organization"),
                 "required": t.get("required", True)}
                for t in self.transit
            ],
            "spots": [{"id": s["id"], "label": s.get("label", s["id"])} for s in self.spots],
        }


def load_all(include_disabled=False):
    """舞台の設定を読む。

    `enabled: false` の舞台は**画面のボタンだけの見出し**で、データを 1 件も持たない。
    既定で外すのは、データ整備のスクリプト（fetch_data.py など）が
    中身の無い舞台を作りにいって落ちないようにするため。
    舞台の一覧を画面に出すときだけ include_disabled=True で拾う。
    """
    stages = {}
    for path in sorted(STAGE_DIR.glob("*.yaml")):
        conf = yaml.safe_load(path.read_text(encoding="utf-8"))
        stage = Stage(conf)
        if stage.enabled or include_disabled:
            stages[stage.id] = stage
    return stages


def load(stage_id=None):
    stages = load_all()
    if stage_id and stage_id in stages:
        return stages[stage_id]
    return stages[sorted(stages)[0]]


def _walk_db_path(self):
    return REPO_ROOT / "data" / f"{self.id}.walk.sqlite"


Stage.walk_db_path = property(_walk_db_path)


def _transfer_db_path(self):
    return REPO_ROOT / "data" / f"{self.id}.transfers.sqlite"


Stage.transfer_db_path = property(_transfer_db_path)
