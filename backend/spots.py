"""子育てスポットの検索。

**自宅から半径 N メートルの中にあるスポットだけを出す**のがこの作品の入口
（2026-09-27・ユーザー指示）。**行き先さがしに移動手段は使わない。**
以前は到達圏（自宅から N 分で行ける範囲）で絞っていたが、
手段によって出る行き先が変わるのが分かりにくかった。半径なら誰が見ても同じ範囲を指す。
移動手段の話は、行き先を決めたあとの**経路探索の側**に寄せてある。
到達圏で絞る道（`reaches=`）も残してあるので、戻したくなったらそのまま使える。

入園・入会の手続きが要る施設（保育所・幼稚園・学童クラブ）は
**取り込みの段階で落としてある**ので、ここには入ってこない。

**地図に出すのは「web で中身を確かめたスポット」だけ**（2026-09-23・ユーザー指示）。
名前と座標しか分からない場所は、行き先として出しても選べない。
判定は `has_web_info()`＝**公式ページの URL を持っているか**で、
出さなかった件数は画面の「使えるデータ」に理由つきで並べる。

データは 2 つある。どちらも公式ページつき。

- `<stage>.json`          市のオープンデータ＋市ホームページからの自前整備
- `<stage>_outside.json`  **市の外**の行き先。公式ページを 1 件ずつ当たって選んだもの
  （**半径の円も到達圏も市境で止まらない**。以前は OpenStreetMap で埋めていたが、
  **OSM にしか情報が無い公園は落とした**。OSM から引き継ぐのは位置と広さだけ）
"""
import json
import math
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATED = REPO_ROOT / "data" / "spots" / "generated"

OUTDOOR_CATEGORIES = ("公園", "緑地", "広場")
PARK_TYPES = ("公園", "緑地", "広場")


def has_web_info(spot):
    """**中身を web で確かめてあるか。** 確かめた印は「公式ページを持っていること」。

    オープンデータの CSV に入っているのは名称・所在地・座標・電話だけで、
    「そこに何があるか」は 1 件も入っていない。市区町村のホームページを 1 件ずつ
    当たって事実（遊具・広さ・トイレ・利用時間）と公式ページを自前整備してあり、
    **そろっていないものは地図に出さない**。
    """
    return bool((spot.get("official_url") or "").strip())


# --- 公園の選定 ---
#
# **公園が多すぎる。** 流山市だけで 429 件。全部出すと地図が公園で埋まり、
# 「わざわざ行く公園」と「近所の遊び場」の区別が付かない。
# 流山市の公園は**広さの中央値が 717m²**（20m×35m くらい）で、ほとんどが街区公園。
# 広さ・遊具・トイレで 4 つに分け、**近ければ小さくても出し、遠いところは大きいものだけ**出す。
BIG_PARK_WORDS = ("総合運動", "運動公園", "総合公園", "地区公園", "近隣公園", "運動場",
                  "水辺公園", "市民の森", "こどもの森", "子どもの森", "セントラルパーク",
                  "森の公園", "自然公園", "河川敷")
MAJOR_AREA_M2 = 10000      # 1ha。ここから上は「遊びに行く」広さ
NORMAL_AREA_M2 = 3000
# (ここまでの距離, 出す階級)。手前から順に見る。
#   〜600m   家のすぐそば。近所の公園は小さくても行き先になるので全部出す
#   〜1.5km  ベビーカーで歩ける距離。**広い・遊具があると分かっているもの**だけ
#   それ以上 わざわざ行く公園だけ
# `unknown`（広さも遊具も分からない公園）を 600m より遠くで出さないのは、
# **「小さい」と決めつけたからではなく、行く理由を示せないから**。
# 落とした件数は画面に出して、公園のオープンデータに広さが無いことも一緒に見せる。
PARK_DISTANCE_RULE = [(600, {"major", "normal", "unknown", "minor"}),
                      (1500, {"major", "normal"}),
                      (None, {"major"})]


def park_toys(spot):
    """遊具の数。市の施設ページから取った `playground`（「すべり台・ブランコ」）を数える。"""
    text = (spot.get("playground") or "").strip()
    return len([x for x in text.split("・") if x]) if text else 0


def park_rank(spot):
    """公園を 4 つに分ける。

    - `major`   わざわざ行く公園（1ha 以上／遊具がそろっていてトイレもある／大きい名前）
    - `normal`  ついでに寄る公園（3,000m² 以上／遊具が 3 種類以上／遊具 2 種類＋トイレ）
    - `minor`   **小さいと分かっている**公園（近所の遊び場）
    - `unknown` 広さも遊具も分からない公園。**小さいとは決めつけない**
    """
    area = spot.get("area_m2") or 0
    toys = park_toys(spot)
    toilet = bool(spot.get("toilet"))
    big_name = any(w in (spot.get("name") or "") for w in BIG_PARK_WORDS)
    # 名前だけでは「わざわざ行く」と言い切れない（「◯◯近隣公園」でも 900m² のことがある）。
    # **広さかトイレの裏づけがあるときだけ** major にする
    if area >= MAJOR_AREA_M2 or (toys >= 4 and toilet) \
            or (big_name and (area >= NORMAL_AREA_M2 or toilet)):
        return "major"
    # **トイレがあるだけでは行く理由にならない。** 公園のトイレは「ある」ほうが多く
    # （実測した区では 182 件中 137 件）、トイレ単独を条件に入れると絞れない。
    # 行く理由になるのは「広さ」か「遊具」で、トイレはそれを後押しする材料として見る
    if area >= NORMAL_AREA_M2 or toys >= 3 or (toys >= 2 and toilet):
        return "normal"
    if area:
        return "minor"
    return "unknown"


def park_is_worth_going(spot, distance_m):
    """その距離なら、この公園を行き先として出すか。"""
    rank = spot.get("park_rank")
    if not rank:
        return True
    for limit, ranks in PARK_DISTANCE_RULE:
        if limit is None or distance_m <= limit:
            return rank in ranks
    return True

# 「今日、子どもとどこかに行きたい」のときに、まず見せたい順
PICK_ORDER = ["子育て支援センター", "子育てひろば", "子ども家庭支援センター",
              "児童館・児童センター", "イベント", "図書館", "公民館", "公園"]


def _event_on(spot, on_date):
    """イベントが、その日（以降）に開かれるか。日付が無いものは通す。"""
    start, end = spot.get("event_start"), spot.get("event_end")
    if not start:
        return True
    return (end or start) >= on_date.isoformat()


def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p = math.pi / 180
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


class SpotIndex:
    def __init__(self, stage):
        self.stage = stage
        self.spots = []
        self.sources = []
        self.details = {}
        self.outside = []
        self.outside_meta = {}
        path = GENERATED / f"{stage.id}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.spots = data.get("spots", [])
            self.sources = data.get("sources", [])
            self.details = data.get("details", {})
        out_path = GENERATED / f"{stage.id}_outside.json"
        out_data = {}
        if out_path.exists():
            out_data = json.loads(out_path.read_text(encoding="utf-8"))
            self.outside = out_data.get("spots", [])

        # **画面に出さない種類は、ここで落とす**（舞台の設定 `hide_types`）。
        # データは `data/spots/generated/*.json` に残したままで、行き先として出すのをやめるだけ。
        # **市外の行き先の内訳（下）より先に落とす**。あとにすると
        # 「市外の行き先 929 件」と言いながら 21 件しか出さない、という食い違いになる
        self.hidden_types = {}
        hide = set(getattr(stage, "hide_types", []) or [])
        if hide:
            for group in ("spots", "outside"):
                items = getattr(self, group)
                for item in items:
                    if self._is_hidden(item, hide):
                        key = item.get("facility_type") or item.get("category") or "その他"
                        self.hidden_types[key] = self.hidden_types.get(key, 0) + 1
                setattr(self, group, [i for i in items if not self._is_hidden(i, hide)])

        # 市外の行き先の内訳は、**残ったものから数え直す**（作った時点の数字をそのまま
        # 使うと、隠した種類のぶんがずれる）。件数に関係しない欄だけファイルから引き継ぐ
        if out_path.exists():
            self.outside_meta = {"by_city": self._tally(self.outside, lambda s: s.get("city") or "市外"),
                                 "by_type": self._tally(self.outside,
                                                        lambda s: s.get("facility_type") or "その他"),
                                 "count": len(self.outside),
                                 "cities": len({s.get("city") for s in self.outside}),
                                 "osm_candidates": out_data.get("osm_candidates", 0),
                                 "hand": self._hand_count(self.outside),
                                 "from_dataset": len(self.outside) - self._hand_count(self.outside),
                                 "source_note": self._outside_note(self.outside)}

        # **web で中身を確かめていないものは持たない。** 件数だけ理由として残す
        self.no_web_info = {}
        for group in ("spots", "outside"):
            items = getattr(self, group)
            for item in items:
                if not has_web_info(item):
                    key = item.get("facility_type") or item.get("category") or "その他"
                    self.no_web_info[key] = self.no_web_info.get(key, 0) + 1
            setattr(self, group, [i for i in items if has_web_info(i)])
        self.by_id = {s["id"]: s for s in self.spots}
        self.by_id.update({s["id"]: s for s in self.outside})
        for s in self.spots + self.outside:
            if s.get("indoor") is None:
                s["indoor"] = not any(c in (s.get("category") or "") for c in OUTDOOR_CATEGORIES)
            # 公園の階級は広さ（`area_m2`）から毎回決める。データに焼き込まない。
            # ただし**市外の行き先だけは、書いてある階級を優先する**
            # （隣の市が「代表的な公園」として挙げているなら、広さが分からなくても出す）
            if (s.get("facility_type") or "") in PARK_TYPES:
                s["park_rank"] = s.get("park_rank") or park_rank(s)

    # 市外の行き先を数え直すための小道具（`scripts/build_outside_spots.py` と同じ数え方）
    HAND_LABEL = "自前整備（市外の行き先）"

    @staticmethod
    def _tally(items, key):
        out = {}
        for s in items:
            k = key(s)
            out[k] = out.get(k, 0) + 1
        return sorted(out.items(), key=lambda x: -x[1])

    @classmethod
    def _hand_count(cls, items):
        """公式ページを 1 件ずつ当たって作った分（残りはデータセットから取った分）。"""
        return sum(1 for s in items if s.get("source_label") == cls.HAND_LABEL)

    @classmethod
    def _outside_note(cls, items):
        hand = cls._hand_count(items)
        parts = []
        if hand:
            parts.append(f"隣の市区町村が自分のホームページで載せている行き先を 1 件ずつ当たったもの {hand} 件")
        if len(items) - hand:
            parts.append("区市町村をまたいで配られているデータセットから取ったもの "
                         f"{len(items) - hand} 件")
        return "／".join(parts)

    @staticmethod
    def _is_hidden(spot, hide):
        """その種類を画面から外してあるか。

        見出しの種類（`facility_type`）だけで判断する。**出どころでは判断しない。**
        ほこナビのトイレのデータには「公園の中のおむつ替えトイレ」が混ざっていて、
        取り込みのときに公園と統合されて `facility_type: 公園` になっているものがある
        （港区で 40 件・流山市で 10 件）。出どころで落とすとその公園まで消えてしまう。
        """
        return (spot.get("facility_type") or "") in hide

    def search(self, lat, lon, profile, radius_km=10, limit=400, categories=None,
               on_date=None, reach=None, reaches=None):
        """**今日ふらっと行ける場所**を返す。

        既定は **`radius_km` の円**（自宅からの直線距離）で絞る。**移動手段は使わない。**
        市の外のスポットも混ぜる（**円は市境で止まらない**ので、隣の市の行き先も候補になる）。

        `reaches`（到達圏の並び）を渡すと、円の代わりに**そのどれかに入るスポットだけ**を
        返し、各スポットの `reach` に届く手段の id（walk / bike / transit）を入れる。
        いまの画面は使っていないが、戻せるように残してある。
        `reach` は 1 つだけ渡すときの書き方。

        公園は**遠いほど大きいものだけ**に絞る（`park_is_worth_going`）。
        落とした件数は `last_skipped_parks` に入れて、画面で「近所の小さな公園は
        遠いので出していません」と言えるようにする。
        """
        if reaches is None and reach is not None:
            reaches = [reach]
        out = []
        skipped_parks = 0
        for s in self.spots + self.outside:
            if not profile.spot_matches(s):
                continue
            if categories and s.get("facility_type") not in categories \
                    and s.get("category") not in categories:
                continue
            if s.get("dropin") is False:        # 取り込みで落としてあるが念のため
                continue
            if s.get("event_start") and on_date and not _event_on(s, on_date):
                continue
            d = haversine(lat, lon, s["lat"], s["lon"])
            if not park_is_worth_going(s, d):
                skipped_parks += 1
                continue
            hit = None
            if reaches:
                hit = [r.mode for r in reaches if r.contains(s["lat"], s["lon"])]
                if not hit:
                    continue
            elif d > radius_km * 1000:
                continue
            item = dict(s)
            item["distance_m"] = int(d)
            if hit is not None:
                item["reach"] = hit
            out.append(item)
        out.sort(key=lambda x: x["distance_m"])
        self.last_skipped_parks = skipped_parks
        return out[:limit], len(out)

    def get(self, spot_id):
        return self.by_id.get(spot_id)

    def categories(self, items=None):
        seen = {}
        for s in (self.spots if items is None else items):
            if s.get("dropin") is False:
                continue
            c = s.get("facility_type") or s.get("category") or "その他"
            seen[c] = seen.get(c, 0) + 1
        return sorted(seen.items(), key=lambda x: -x[1])

    def picks(self, lat, lon, profile, radius_km=10, limit=6, on_date=None,
              reach=None, reaches=None):
        """左ペインに出す「近くの行き先」。**その日ふらっと行ける施設**を種類の順で。

        トイレや授乳スペースは行き先そのものではないので、ここには出さない。
        """
        keep = getattr(self, "last_skipped_parks", 0)
        found, _ = self.search(lat, lon, profile, radius_km=radius_km, limit=4000,
                               on_date=on_date, reach=reach, reaches=reaches)
        self.last_skipped_parks = keep      # 落とした公園の件数は search のものを残す
        by_kind = {}
        for s in found:
            by_kind.setdefault(s.get("facility_type"), []).append(s)
        out = []
        # 種類の順に 1 つずつ取っていく（支援センターだけで埋まらないように）
        for round_ in range(3):
            for kind in PICK_ORDER:
                items = by_kind.get(kind) or []
                if len(items) > round_:
                    out.append(items[round_])
                if len(out) >= limit:
                    return out
        return out

    def events(self, on_date, limit=20):
        items = [s for s in self.spots if s.get("event_start")]
        items = [s for s in items if _event_on(s, on_date)]
        items.sort(key=lambda s: s.get("event_start") or "9999")
        return items[:limit]

    def park_summary(self):
        """公園の内訳。**広さのあるものが何件あるか**まで出す（CSV には広さが無い）。"""
        parks = [s for s in self.spots if s.get("park_rank")]
        outside = [s for s in self.outside if s.get("park_rank")]
        def ranks(items):
            out = {}
            for s in items:
                out[s["park_rank"]] = out.get(s["park_rank"], 0) + 1
            return out
        return {"inside": len(parks), "outside": len(outside),
                "with_area": sum(1 for s in parks + outside if s.get("area_m2")),
                "ranks": ranks(parks), "outside_ranks": ranks(outside)}

    def coverage(self):
        return {"total": len(self.spots),
                # 持ってはいるが、行き先として出していない種類
                "hidden_types": sorted(self.hidden_types.items(), key=lambda x: -x[1]),
                "hidden_total": sum(self.hidden_types.values()),
                "no_web_info": sorted(self.no_web_info.items(), key=lambda x: -x[1]),
                "no_web_info_total": sum(self.no_web_info.values()),
                "detailed": self.details.get("detailed", 0),
                "closed": self.details.get("closed", 0),
                "closed_names": self.details.get("closed_names", []),
                "with_summary": sum(1 for s in self.spots if s.get("summary")),
                "dropin": sum(1 for s in self.spots if s.get("dropin")),
                # 入園・入会が要る施設は取り込みの段階で落としてある。その件数だけ残す
                "enrollment_excluded": self.details.get("enrollment_excluded", 0),
                "enrollment_types": self.details.get("enrollment_types", []),
                "events": sum(1 for s in self.spots if s.get("event_start")),
                "outside": self.outside_meta,
                "parks": self.park_summary(),
                "sources": self.sources}
