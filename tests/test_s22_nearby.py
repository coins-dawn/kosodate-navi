"""S22 さがす範囲。**自宅から半径 N メートルの中にある行き先**。

2026-09-27・ユーザー指示で、到達圏（[tests/test_s12_reach.py]）から置き換えた。
**行き先さがしに移動手段は使わない。** 手段を替えると出る行き先が変わるのが
分かりにくかったので、直線距離という誰が見ても同じ尺度に寄せた。
移動手段の話は、行き先を決めたあとの経路探索（`/api/routes`）に残っている。

ここで確かめるのは、半径で素直に増えること、円が市境で止まらないこと、
そして**同じ半径なら手段によらず答えが 1 つ**であること。
"""
from datetime import date

import pytest

from backend import profile as profile_mod, spots as spots_mod

ON = date(2026, 9, 24)          # 木曜日（平日）


@pytest.fixture(scope="module")
def idx(nagareyama):
    return spots_mod.SpotIndex(nagareyama)


@pytest.fixture(scope="module")
def home(nagareyama):
    h = nagareyama.homes[0]
    return (h["lat"], h["lon"])


@pytest.fixture(scope="module")
def prof():
    return profile_mod.for_mode("walk")


def search(idx, home, prof, radius_m):
    return idx.search(home[0], home[1], prof, radius_km=radius_m / 1000.0,
                      limit=4000, on_date=ON)


def test_every_spot_is_inside_the_radius(idx, home, prof):
    """**出すのは半径の中にあるものだけ。** 判定は直線距離（haversine）。"""
    items, total = search(idx, home, prof, 2000)
    assert items and total == len(items)
    for s in items:
        assert s["distance_m"] <= 2000
        # 画面は近い順に読むので、距離を必ず持たせる
        assert s["distance_m"] == int(spots_mod.haversine(
            home[0], home[1], s["lat"], s["lon"]))


def test_spots_are_sorted_by_distance(idx, home, prof):
    items, _ = search(idx, home, prof, 3000)
    assert items == sorted(items, key=lambda s: s["distance_m"])


def test_a_wider_radius_is_a_superset(idx, home, prof):
    """**円は入れ子。** 広げたら、狭いときに出ていたものは必ず残る。

    静的版が「いちばん広い半径の行き先だけ経路を作る」のはこの性質に乗っている。
    """
    near, _ = search(idx, home, prof, 1000)
    far, _ = search(idx, home, prof, 3000)
    assert len(far) > len(near)
    assert {s["id"] for s in near} <= {s["id"] for s in far}


def test_the_circle_does_not_stop_at_the_city_border(idx, home, prof):
    """**円は市境で止まらない。** 隣の市の行き先も、届く距離なら候補になる。

    流山おおたかの森は**柏市との市境から 556m** しかない。2026-09-27 に隣の市の
    子育て支援センター・児童館・授乳おむつ替えを自前整備するまで、市外の行き先は
    公園だけで、いちばん近いものが 3.3km 先だった（＝円を広げても市外がすかすかに見えた）。
    """
    assert len(idx.outside) > 50                      # 隣の市の行き先を自前整備してある
    near, _ = search(idx, home, prof, 1500)
    assert [s for s in near if s.get("outside")]       # 1.5km でもう隣の市が出る
    far, _ = search(idx, home, prof, 6000)
    outside = [s for s in far if s.get("outside")]
    assert len(outside) > 40
    assert {s["city"] for s in outside} >= {"柏市", "松戸市"}
    # **公園だけではない。** 行き先の種類が市内と同じようにそろっている
    kinds = {s["facility_type"] for s in outside}
    assert {"子育て支援センター", "児童館・児童センター", "授乳・おむつ替え"} <= kinds


def test_the_answer_does_not_depend_on_the_travel_mode(idx, home):
    """**同じ半径なら、手段を替えても答えは変わらない。** これが置き換えの理由そのもの。"""
    by_mode = [search(idx, home, profile_mod.for_mode(m), 2000)[0]
               for m in ("walk", "bike", "transit", "stroller")]
    ids = [[s["id"] for s in items] for items in by_mode]
    assert all(x == ids[0] for x in ids)
    # 到達圏のときに付けていた「どの手段で届くか」は、もう付かない
    assert all("reach" not in s for s in by_mode[0])


def test_small_parks_far_away_are_still_dropped(idx, home, prof):
    """**公園の絞り込み（600m／1.5km の段）は半径にしても効いている。**

    全部出すと地図が公園で埋まる。落とした件数は画面に理由つきで出す。
    """
    search(idx, home, prof, 6000)
    assert idx.last_skipped_parks > 100


def test_nearby_api(client):
    d = client.get("/api/nearby?stage=nagareyama&home=otakanomori"
                   "&radius_m=2000&date=2026-09-24&time=09:00").get_json()
    assert d["home"]["id"] == "otakanomori"
    assert d["radius_m"] == 2000
    assert d["area_km2"] == pytest.approx(12.57, abs=0.01)   # πr²
    assert d["count"] == len(d["spots"]) and d["spots"]
    assert all(s["distance_m"] <= 2000 for s in d["spots"])
    assert d["inside"] + d["outside"] == d["count"]
    assert d["picks"] and d["categories"]


def test_nearby_api_grows_with_the_radius(client):
    def count(r):
        return client.get(f"/api/nearby?stage=nagareyama&home=otakanomori"
                          f"&radius_m={r}&date=2026-09-24&time=09:00").get_json()["count"]
    assert count(500) < count(1500) < count(4000)


def test_unknown_stage_falls_back_to_a_usable_one(client):
    """**まだ使えない舞台を頼まれても壊れない。** 使える舞台に落として返す。"""
    d = client.get("/api/nearby?stage=shinjuku&radius_m=1500").get_json()
    assert d["home"]["lat"] and d["home"]["lon"]
    assert d["radius_m"] == 1500
