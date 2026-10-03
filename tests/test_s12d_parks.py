"""S12d／S13 公園の選定。**公園が多すぎる**問題への答え。

流山市 429 件の公園を全部地図に出すと、行き先を選べない。
遠くの小さな公園にわざわざ行く人はいないので、

- 広さは **OSM のポリゴンから測る**（市の CSV には広さが無い。新宿区は区の一覧にある）
- 広さ・遊具・トイレで 4 つの階級に分ける（**トイレだけでは行く理由にしない**）
- **近ければ全部出し、遠いところは大きいものだけ出す**（600m／1.5km で段を変える）

ようにした。ここではその筋が通っていることを確かめる。
"""
from backend import profile, spots


def parks_of(idx):
    return [s for s in idx.spots if s.get("park_rank")]


def test_park_area_comes_from_osm_polygons(nagareyama):
    """**公園の広さはオープンデータに無い。** OSM のポリゴンを測って付けている。"""
    idx = spots.SpotIndex(nagareyama)
    parks = parks_of(idx)
    assert len(parks) > 350
    sized = [s for s in parks if s.get("area_m2")]
    assert len(sized) > 250                      # 6 割以上に広さが付く
    biggest = max(sized, key=lambda s: s["area_m2"])
    assert biggest["name"] == "流山市総合運動公園"
    assert biggest["area_m2"] > 100000           # 10ha 超
    assert biggest["park_rank"] == "major"


def test_outside_parks_are_curated_not_osm(nagareyama):
    """**市外の行き先は OSM をやめ、隣の市の公式ページから自前整備した。**

    OSM にあるのは名前と形だけで、行き先を選ぶのに要る中身が無い。
    いまの市外のスポットは**全部が公式ページつき**で、階級も書いてある。
    """
    idx = spots.SpotIndex(nagareyama)
    outside = idx.outside
    assert 10 < len(outside) < 100                 # 1,713 件 → 数十件に絞った
    assert all(s.get("official_url") for s in outside)
    assert all(s.get("summary") for s in outside)
    assert all(s.get("source_id") != "osm" for s in outside)
    parks = [s for s in outside if s.get("park_rank")]
    assert parks and all(s["park_rank"] == "major" for s in parks)
    assert {s["city"] for s in outside} >= {"柏市", "松戸市"}


def test_ranks_are_spread(nagareyama):
    """4 つの階級に散っていること（全部 major や全部 minor になっていない）。"""
    idx = spots.SpotIndex(nagareyama)
    ranks = {}
    for s in parks_of(idx):
        ranks[s["park_rank"]] = ranks.get(s["park_rank"], 0) + 1
    assert set(ranks) == {"major", "normal", "minor", "unknown"}
    assert ranks["major"] > 10
    assert ranks["minor"] > 40
    # **「わざわざ行く公園」は少数でないと意味がない**（多すぎると絞ったことにならない）
    assert ranks["major"] < len(parks_of(idx)) / 10


def test_missing_data_is_not_treated_as_small(nagareyama):
    """**広さも遊具も分からない公園を「小さい」と決めつけない。**

    データを出していないところほど不利になるのを避けるため、`minor` ではなく
    `unknown` にして、落とすときは理由と件数を画面に出す。
    """
    assert spots.park_rank({"name": "なにか公園"}) == "unknown"
    assert spots.park_rank({"name": "なにか公園", "area_m2": 300}) == "minor"
    assert spots.park_rank({"name": "なにか公園", "area_m2": 20000}) == "major"
    # 名前が大きくても、**広さかトイレの裏づけが無ければ major にしない**
    assert spots.park_rank({"name": "なにか近隣公園"}) == "unknown"
    assert spots.park_rank({"name": "なにか近隣公園", "toilet": True}) == "major"
    assert spots.park_rank({"name": "なにか公園", "playground": "すべり台・ブランコ・鉄棒・砂場",
                            "toilet": True}) == "major"
    assert spots.park_rank({"name": "なにか公園",
                            "playground": "すべり台・ブランコ・鉄棒"}) == "normal"
    # **トイレがあるだけでは行く理由にならない**（新宿区は区立公園の 4 分の 3 にトイレがある）
    assert spots.park_rank({"name": "なにか公園", "toilet": True}) == "unknown"
    assert spots.park_rank({"name": "なにか公園", "toilet": True,
                            "playground": "すべり台・ブランコ"}) == "normal"


def test_far_small_parks_are_dropped(nagareyama):
    """**近ければ小さくても出し、遠いところは大きいものだけ出す。**"""
    idx = spots.SpotIndex(nagareyama)
    home = nagareyama.homes[0]
    prof = profile.for_mode("walk")
    items, _ = idx.search(home["lat"], home["lon"], prof, radius_km=10, limit=9000)
    assert idx.last_skipped_parks > 200          # 市内だけでこれだけ落ちる
    for s in items:
        rank = s.get("park_rank")
        if not rank:
            continue
        if s["distance_m"] > 1500:
            assert rank == "major", f"{s['name']} {s['distance_m']}m {rank}"
        elif s["distance_m"] > 600:
            assert rank in ("major", "normal"), f"{s['name']} {rank}"
    # 600m 以内には小さい公園も残っている
    near = [s for s in items if s.get("park_rank") and s["distance_m"] <= 600]
    assert any(s["park_rank"] in ("minor", "unknown") for s in near)


def test_dropping_parks_cuts_the_map_down(nagareyama):
    """選定の前後で、地図に出る件数が実際に減っていること。"""
    idx = spots.SpotIndex(nagareyama)
    home = nagareyama.homes[0]
    items, total = idx.search(home["lat"], home["lon"], profile.for_mode("walk"),
                              radius_km=10, limit=9000)
    parks = [s for s in items if s.get("park_rank")]
    assert len(parks) < len(parks_of(idx)) / 5   # 5 分の 1 以下になる
    assert len(parks) < total                    # 公園だけの地図にはならない


def test_park_rank_reaches_the_api(client):
    """画面が印の大きさを変えられるよう、`park_rank` を API で返している。"""
    d = client.get("/api/nearby?stage=nagareyama&radius_m=3000"
                   "&date=2026-09-24&time=09:00").get_json()
    parks = [s for s in d["spots"] if s.get("park_rank")]
    assert parks
    assert any(s["park_rank"] == "major" for s in parks)
    assert d["skipped_parks"] > 0


# --- S13 web で中身を確かめたスポットだけを出す ---

def test_only_spots_with_web_info_are_shown(nagareyama):
    """**地図に出すのは、公式ページまでそろえたスポットだけ。**

    オープンデータの CSV には名称・所在地・座標・電話しか無い。
    名前しか分からない場所を出しても行き先として選べないので、
    市区町村のページを当たって事実と公式ページをそろえたものだけを残し、
    **そろわなかったものは件数と種類で見せる**。
    """
    idx = spots.SpotIndex(nagareyama)
    assert idx.spots and idx.outside
    assert all(spots.has_web_info(s) for s in idx.spots + idx.outside)
    cov = idx.coverage()
    assert cov["no_web_info_total"] > 0              # 落としたものは必ず数える
    assert cov["no_web_info"][0][1] > 0


def test_has_web_info_is_the_official_page(nagareyama):
    assert not spots.has_web_info({"name": "名前しか無い公園"})
    assert not spots.has_web_info({"name": "空欄", "official_url": "  "})
    assert spots.has_web_info({"name": "あり", "official_url": "https://example.lg.jp/park"})
