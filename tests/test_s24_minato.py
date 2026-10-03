"""S24 2 つめの舞台・港区。**流山市で「なし」だった層が、ここで初めて実データで効く。**

横展開先を実測で選んだ結果（contexts/agent/kosodate-navi-next-stage.md）、
ほこナビの屋外歩行空間と都営地下鉄の GTFS-Pathways が**同時にそろう唯一の区**として
港区を選んだ。このテストが確かめるのは、その 2 つが本当に効いていることと、
**港区の施設情報オープンデータだけで「web で中身を確かめたスポット」の条件を満たせる**こと。
"""
from datetime import date

import pytest

from backend import (barrier as barrier_mod, gtfs, indoor as indoor_mod,
                     profile as profile_mod, spots as spots_mod)

ON = date(2026, 9, 24)          # 木曜日（平日）


@pytest.fixture(scope="module")
def idx(minato):
    return spots_mod.SpotIndex(minato)


@pytest.fixture(scope="module")
def raw_idx(minato):
    """**隠す前の索引。** `hide_types`（舞台の設定）は画面に出すのをやめるだけで、
    データは `data/spots/generated/` に残っている。**取り込みが正しいかを見るテストは
    こちらを見る**。画面に何が出るかを見るテストは `idx` と API を見る。
    """
    hidden = minato.hide_types
    minato.hide_types = []
    try:
        return spots_mod.SpotIndex(minato)
    finally:
        minato.hide_types = hidden


@pytest.fixture(scope="module")
def con(minato):
    return gtfs.connect(minato.db_path)


def test_area_and_home(minato):
    """区域は国土数値情報 N03 から切り出したもの。自宅は区の中に置く。"""
    assert round(minato.area_km2(), 1) == 20.8
    assert minato.homes and minato.homes[0]["id"] == "shibaura"
    h = minato.homes[0]
    assert minato.contains(h["lat"], h["lon"])


def test_home_is_not_right_next_to_a_station(minato, con):
    """**自宅は駅から離して置く**（2026-09-27・ユーザー指示）。

    流山市は駅のすぐ横に置いてしまい、歩き始めがいつも駅前だった。
    港区は区内を 100m 格子で測ると**最寄り駅までの中央値が 342m**しかないので、
    「ほどよく離れている」は 700m 以上を目安にした（区の 7% しかない範囲）。
    ここでは取り込んだ鉄道の駅（都営・メトロ・JR）で測る。
    """
    h = minato.homes[0]
    cur = con.cursor()
    rows = cur.execute(
        "SELECT lat, lon FROM stop WHERE feed_id IN ('toei_train','metro','jreast')").fetchall()
    assert rows
    nearest = min(spots_mod.haversine(h["lat"], h["lon"], r["lat"], r["lon"]) for r in rows)
    # 取り込んだ鉄道（都営・メトロ・JR）でいちばん近いのは高輪ゲートウェイの 876m。
    # ゆりかもめは ODPT に無いので、いちばん近い駅は実際には芝浦ふ頭の約 860m
    assert nearest > 700, f"自宅が駅に近すぎる（{nearest:.0f}m）"


def test_hokonavi_is_available_here(minato):
    """**駅の外の段差データ。** 流山市では 0km だった層が、港区では 38.2km 入る。"""
    layer = barrier_mod.BarrierLayer(minato)
    cover = layer.coverage()
    assert cover["available"] is True
    assert cover["km"] > 30
    assert {d["name"] for d in cover["datasets"]} == {
        "nwd_daimon_station", "nwd_odaiba-kaihin-koen_station"}


def test_station_interior_is_available_here(minato, con):
    """**駅の中。** 大江戸線 12 駅の GTFS-Pathways。流山市では 0 駅だった。"""
    net = indoor_mod.IndoorNetwork(con, "toei_pathways")
    assert net.available is True
    s = net.summary()
    assert s["stations"] == 12 and s["entrances"] == 67
    # 港区内は 5 駅（大門・赤羽橋・麻布十番・六本木・青山一丁目）
    inside = [v for v in net.stations.values() if minato.contains(v["lat"], v["lon"])]
    assert len(inside) == 5


def test_daimon_connects_the_inside_to_the_outside(minato, con):
    """**大門駅は、出入口のほとんどが屋外の歩行空間ネットワークとつながる。**

    ここが港区を選んだ決め手。「ホーム → 改札 → 出入口 → 歩道 → 目的地」を
    1 本の段差なし経路にできるのは、調べた限りこの駅だけだった。

    **アプリ自身の当てはめ（`BarrierLayer.lookup` は区間の中点まで 25m）で 13 か所中 12 か所。**
    選定のときに測った「13/13」は、**屋外ネットワークの節点まで 50m** というゆるい条件だった。
    ほかの 4 駅は屋外データが無いので 0〜1 か所にとどまる（港区の中でも差が出る）。
    """
    net = indoor_mod.IndoorNetwork(con, "toei_pathways")
    layer = barrier_mod.BarrierLayer(minato)
    sid = [k for k, v in net.stations.items() if v["stop_name"] == "大門"][0]
    pts = net.entrance_points(sid)
    assert len(pts) == 13
    assert net.step_free_entrances(sid)          # 段差なしで入れる出入口がある
    near = sum(1 for _, la, lo in pts if layer.lookup(la, lo))
    assert near >= 12, f"屋外とつながるのは {near}/13"
    # 屋外データが無い駅は、つながらないことがそのまま出る
    roppongi = [k for k, v in net.stations.items() if v["stop_name"] == "六本木"][0]
    assert not any(layer.lookup(la, lo) for _, la, lo in net.entrance_points(roppongi))


def test_spots_come_with_official_pages_from_open_data(raw_idx):
    """**港区は公式ページ URL がオープンデータに入っている。**

    流山市では市のホームページを 1 件ずつ当たって自前整備するしかなかったが、
    港区の施設情報データは `ファイルパス` に施設ごとのページを持っている。
    """
    assert len(raw_idx.spots) > 250
    with_url = [s for s in raw_idx.spots if (s.get("official_url") or "").startswith("http")]
    assert len(with_url) / len(raw_idx.spots) > 0.9


def test_enrollment_only_facilities_are_dropped(idx):
    """保育園・幼稚園・学童クラブは行き先にならないので取り込まない。"""
    names = [s["name"] for s in idx.spots]
    assert not [n for n in names if "学童クラブ" in n]
    assert not [n for n in names if n.endswith("保育園") or n.endswith("幼稚園")]


def test_diaper_toilets_come_from_the_tokyo_barrier_free_data(raw_idx):
    """**おむつ替えのできるトイレ。** 流山市は 2 件しか無かったが、港区は 136 件。

    出どころはほこナビのバリアフリー施設等データ（東京都・5,692 施設）で、
    `lgCode` で港区を切り、**おむつ交換台があるものだけ**を採っている。
    **2026-09-30 から画面には出していない**（下の `test_diaper_toilets_are_kept_but_not_shown`）
    が、取り込みは今までどおり動いていること。
    """
    toilets = [s for s in raw_idx.spots if s["facility_type"] == "おむつ替えのできるトイレ"]
    # 取り込みは 136 件。うち 45 件は同じ場所の区民センターや図書館とまとめられるので、
    # 単独のスポットとして残るのは 91 件（まとめた先には設備として付いている）
    assert len(toilets) > 80
    assert all(s.get("diaper_table") for s in toilets)


def test_park_area_comes_from_the_description_text(idx):
    """**港区の公園は、広さが「施設の概要」の文章の中に書いてある**（列ではない）。"""
    parks = [s for s in idx.spots if s["facility_type"] == "公園"]
    assert len(parks) > 150
    with_area = [s for s in parks if s.get("area_m2")]
    assert len(with_area) > 120
    assert [s for s in parks if s.get("park_rank") == "major"]


def test_nearby_api(client):
    d = client.get("/api/nearby?stage=minato&home=shibaura"
                   "&radius_m=2000&date=2026-09-24&time=10:00").get_json()
    assert d["home"]["id"] == "shibaura"
    assert d["count"] > 40
    kinds = {s["facility_type"] for s in d["spots"]}
    assert {"公園", "児童館・児童センター", "子育てひろば", "区民センター"} <= kinds
    # おむつ替えのできるトイレは**行き先として出さない**（2026-09-30・ユーザー指示）
    assert "おむつ替えのできるトイレ" not in kinds


def test_coverage_api_shows_both_layers(client):
    d = client.get("/api/coverage?stage=minato").get_json()
    assert d["hokonavi"]["available"] is True
    assert d["indoor"]["available"] is True


# --- S25 隣の区の行き先（2026-09-27） ---

def test_neighbouring_wards_are_covered(raw_idx):
    """**半径 6km の円は区境で止まらない。** 隣の区の行き先も候補になる。

    自宅（芝浦四丁目）から区境までは品川区 1,235m／中央区 2,211m／江東区 2,411m／
    渋谷区 2,613m／目黒区 2,968m／千代田区 3,583m（国土数値情報 N03 で実測）。
    """
    assert len(raw_idx.outside) > 900
    cities = {s["city"] for s in raw_idx.outside}
    assert {"品川区", "中央区", "江東区", "渋谷区", "目黒区", "千代田区"} <= cities
    assert all(s.get("outside") for s in raw_idx.outside)
    assert all((s.get("official_url") or "").startswith("http") for s in raw_idx.outside)


def test_outside_toilets_come_from_one_tokyo_wide_dataset(raw_idx):
    """**東京都のバリアフリー施設等データは 63 区市町村ぶんが 1 つに入っている。**

    だから `lg_codes` を並べるだけで隣の区まで届く。流山市では隣の市の公式ページを
    62 件ぶん手で当たったが、ここではその作業が要らない。
    """
    toilets = [s for s in raw_idx.outside if s["facility_type"] == "おむつ替えのできるトイレ"]
    assert len(toilets) > 880
    assert all(s.get("diaper_table") for s in toilets)


def test_outside_has_more_than_one_kind(idx, raw_idx):
    """**一種類だけにしない。** 流山市で「市外が公園だけ」になった失敗を繰り返さない。

    隣の区の児童館は、品川区＝区のページを 1 件ずつ、中央区＝区のページ＋区のオープンデータの座標、
    江東区＝区のオープンデータ（URL 列が 262 件中 261 件埋まっている）から作った。
    """
    kinds = {s["facility_type"] for s in raw_idx.outside}
    assert {"おむつ替えのできるトイレ", "児童館・児童センター"} <= kinds
    halls = [s for s in idx.outside if s["facility_type"] == "児童館・児童センター"]
    assert len(halls) == 21
    assert {s["city"] for s in halls} == {"品川区", "中央区", "江東区"}
    # 時間まで入れてある（中央区は「小学生以下は午後5時まで」を閉館時刻にしている）
    assert all(s.get("open_time") and s.get("close_time") for s in halls)


def test_nearby_api_crosses_the_ward_border(client):
    d = client.get("/api/nearby?stage=minato&home=shibaura"
                   "&radius_m=6000&date=2026-09-24&time=10:00").get_json()
    outside = [s for s in d["spots"] if s.get("outside")]
    # おむつ替えのできるトイレを出さなくなったので、残る市外の行き先は
    # **公式ページを 1 件ずつ当たって作った隣の区の児童館 21 件**（2026-09-30）
    assert len(outside) == 21
    assert d["outside"] == len(outside)
    assert dict(d["outside_cities"]) == {"品川区": 10, "中央区": 6, "江東区": 5}
