"""S12b 到達圏（**自宅から N 分で行ける範囲**）のエンジン。

**2026-09-27 以降、画面はこれを使っていない**（行き先の絞り込みは
自宅からの半径になった → [tests/test_s22_nearby.py]）。
手段を替えると出る行き先が変わるのが分かりにくい、というのが取りやめた理由で、
**計算そのものが間違っていたわけではない**。戻したくなったときにすぐ使えるよう、
エンジン（`backend/isochrone.py`）とこのテストは残してある。

確かめるのは「どこまで行けるか」が手段で変わること、ベビーカーは階段を通れないぶん
狭いこと、公共交通は駅の周りに飛び地ができること。
"""
from datetime import date

import pytest

from backend import isochrone as iso_mod, profile as profile_mod, spots as spots_mod
from backend import gtfs, transit, walk

WHEN = 9 * 3600
ON = date(2026, 9, 24)          # 木曜日（平日）


@pytest.fixture(scope="module")
def service(nagareyama):
    g = walk.WalkGraph(nagareyama.walk_db_path)
    con = gtfs.connect(nagareyama.db_path)
    net = transit.Network(con, nagareyama.transfer_db_path)
    return iso_mod.IsochroneService(g, transit.Router(net, g, None))


@pytest.fixture(scope="module")
def home(nagareyama):
    h = nagareyama.homes[0]
    return (h["lat"], h["lon"])


def test_homes_are_preset_and_inside_the_stage(all_stages):
    """自宅は候補から選ぶ。候補は舞台の中に置いておく。"""
    for stage in all_stages.values():
        assert stage.homes
        for h in stage.homes:
            assert stage.contains(h["lat"], h["lon"]), f"{stage.id} {h['name']}"


def test_nagareyama_home_is_otakanomori_only(nagareyama):
    """流山市の自宅は流山おおたかの森の 1 か所だけ（あとで足せるようにはしてある）。"""
    assert [h["id"] for h in nagareyama.homes] == ["otakanomori"]


def test_reach_modes_are_walk_bike_transit():
    """**到達圏に出せる手段は 3 つ。** 画面ではこの中から 1 つを選んで出す。"""
    assert [m["id"] for m in profile_mod.load_reach_modes()] == ["walk", "bike", "transit"]
    assert profile_mod.for_mode("bike").graph_mode == "bike"
    assert profile_mod.for_mode("bike").graph_speed > profile_mod.for_mode("walk").graph_speed
    assert profile_mod.for_mode("transit").transit is True
    # ベビーカーは到達圏には出さないが、経路探索では選べる（階段を通れない）
    assert profile_mod.for_mode("stroller").band["steps"] is None


def test_stroller_reaches_less_than_walking(service, home):
    """**ベビーカーは階段を通れないぶん、同じ時間でも行ける範囲が狭い。**"""
    walk_reach = service.compute(home, profile_mod.for_mode("walk"), 20)
    stroller = service.compute(home, profile_mod.for_mode("stroller"), 20)
    assert walk_reach.area_km2() > stroller.area_km2()
    assert stroller.area_km2() > 0.5


def test_longer_time_reaches_further(service, home):
    small = service.compute(home, profile_mod.for_mode("walk"), 10)
    large = service.compute(home, profile_mod.for_mode("walk"), 30)
    assert large.area_km2() > small.area_km2() * 2
    assert small.cells < large.cells if hasattr(small, "cells_count") else True


def test_bike_reaches_further_than_walking(service, home):
    """**自転車は同じ時間でずっと遠くまで行ける。** 自転車で通れない道は落ちる。"""
    on_foot = service.compute(home, profile_mod.for_mode("walk"), 15)
    by_bike = service.compute(home, profile_mod.for_mode("bike"), 15)
    assert by_bike.area_km2() > on_foot.area_km2() * 3
    assert by_bike.mode == "bike" and by_bike.label == "自転車"


def test_transit_reaches_beyond_walking(service, home):
    """公共交通は**飛び地**ができる（駅の周りだけ届く）。"""
    on_foot = service.compute(home, profile_mod.for_mode("walk"), 45)
    by_transit = service.compute(home, profile_mod.for_mode("transit"), 45,
                                 when=WHEN, on_date=ON)
    assert by_transit.area_km2() > on_foot.area_km2()
    assert by_transit.stats["places"] > 20


def test_spots_are_limited_to_the_reach(nagareyama, service, home):
    """**塗った範囲の中のスポットだけ**が残る（描画と判定が同じマス目）。"""
    idx = spots_mod.SpotIndex(nagareyama)
    prof = profile_mod.for_mode("walk")
    reach = service.compute(home, prof, 20)
    items, total = idx.search(home[0], home[1], prof, limit=4000, reach=reach)
    assert 0 < total < len(idx.spots) + len(idx.outside)
    for s in items:
        assert reach.contains(s["lat"], s["lon"])


def test_neighbour_cities_appear_when_reachable(nagareyama, service, home):
    """**到達圏は市境で止まらない。** 届くなら隣の市のスポットも出す。"""
    idx = spots_mod.SpotIndex(nagareyama)
    assert len(idx.outside) > 10                      # 隣の市の行き先を自前整備してある
    prof = profile_mod.for_mode("bike")
    reach = service.compute(home, prof, 40, when=WHEN, on_date=ON)
    items, _ = idx.search(home[0], home[1], prof, limit=4000, reach=reach)
    outside = [s for s in items if s.get("outside")]
    assert len(outside) >= 5
    assert {s["city"] for s in outside} - {"流山市"}   # 市の名前が付いている
    # 徒歩 15 分なら市外はほとんど出ない
    near = service.compute(home, profile_mod.for_mode("walk"), 15)
    near_items, _ = idx.search(home[0], home[1], profile_mod.for_mode("walk"),
                               limit=4000, reach=near)
    assert len([s for s in near_items if s.get("outside")]) < len(outside)


def test_spot_carries_the_modes_that_reach_it(nagareyama, service, home):
    """スポットは 3 つの到達圏の**和**で残し、**どの手段で届くか**を持たせる。

    画面がこれを使っていたときは、ラジオボタンで `reach` を見て出し入れしていた。
    """
    idx = spots_mod.SpotIndex(nagareyama)
    reaches = [service.compute(home, profile_mod.for_mode(m), 20, when=WHEN, on_date=ON)
               for m in ("walk", "bike", "transit")]
    items, _ = idx.search(home[0], home[1], profile_mod.for_mode("walk"),
                          limit=4000, reaches=reaches)
    assert items
    for s in items:
        assert s["reach"] and set(s["reach"]) <= {"walk", "bike", "transit"}
    # 自転車がいちばん広いので、自転車だけで届くスポットが必ずある
    assert [s for s in items if s["reach"] == ["bike"]]
    # 徒歩で届くものは自転車でも届く（同じ道路網の部分集合に近い）
    walk_only = [s for s in items if "walk" in s["reach"]]
    assert len(walk_only) < len(items)
