"""S26 ほこナビを歩行コストに入れる。**注記から、道の選び方そのものへ。**

2026-09-28 まで、ほこナビは経路を引いたあとの注記
（「歩道 N 区間のうち M 区間で段差なしが確認できました」）にしか使っていなかった。
`BarrierLayer.annotate()` は数えるだけで、**どの道を通るかには一切効いていなかった。**

ここで確かめるのは
  - 仕様書どおりのコード表になっていること（2026-09-28 に 2 か所の誤りを直した）
  - ほこナビの属性が歩行グラフの辺に貼られていること
  - **同じ 2 点でも、ほこナビがあると経路が変わること**
"""
import sqlite3

import pytest

from backend import barrier as barrier_mod, profile as profile_mod, stages as stages_mod, walk


def test_code_tables_match_the_specification():
    """**歩行空間ネットワークデータ整備仕様（2024年7月）の表 3.2 どおり。**

    2026-09-28 に 2 か所直した。
      - `WIDTH` の区切り（2 は 1.0〜2.0m 未満、4 は 3.0m 以上）
      - `LEV_DIFF` に 4（5〜10cm）と 5（10cm超）が抜けていた
    """
    assert barrier_mod.LEV_DIFF["4"] == "5〜10cm"
    assert barrier_mod.LEV_DIFF["5"] == "10cm超"
    assert barrier_mod.WIDTH["2"] == "1〜2m" and barrier_mod.WIDTH["4"] == "3m以上"
    assert barrier_mod.ROOF == {"1": "なし", "2": "あり", "99": "不明"}
    assert barrier_mod.ROUTE_TYPE["6"] == "階段" and barrier_mod.ROUTE_TYPE["4"] == "エレベーター"


def test_roof_code_is_not_read_backwards(minato):
    """**1 は「屋根なし」。** 以前は 1 を「屋根あり」と数えていた（港区の 2,336 区間を誤って）。"""
    layer = barrier_mod.BarrierLayer(minato)
    roofed = sum(1 for items in layer.index.values() for _, i in items if i["roof"] == "2")
    none = sum(1 for items in layer.index.values() for _, i in items if i["roof"] == "1")
    assert roofed == 206 and none == 2336


def test_barrier_edges_are_precomputed(minato):
    """**探索のたびに座標で引くと間に合わない**ので、辺に先に貼ってある。"""
    con = sqlite3.connect(minato.walk_db_path)
    n = con.execute("SELECT count(*) FROM barrier_edge").fetchone()[0]
    assert n > 3000
    # 10cm を超える段差がちゃんと入っている
    bad = con.execute("SELECT count(*) FROM barrier_edge WHERE lev_diff='5'").fetchone()[0]
    assert bad > 100
    # 車も通る道には貼らない（ほこナビは歩道のデータなので）
    mixed = con.execute("SELECT count(*) FROM barrier_edge b JOIN edge e "
                        "ON e.a=b.a AND e.b=b.b WHERE e.car=1").fetchone()[0]
    assert mixed == 0


def test_stage_without_hokonavi_has_an_empty_table(nagareyama):
    """**持っていない舞台でも壊れない。** 流山市はほこナビが 0km なので表は空。"""
    con = sqlite3.connect(nagareyama.walk_db_path)
    assert con.execute("SELECT count(*) FROM barrier_edge").fetchone()[0] == 0


def test_the_graph_attaches_barrier_to_edges(minato):
    g = walk.WalkGraph(minato.walk_db_path)
    assert g.has_barrier is True
    g.load_area([(35.6565, 139.7553)], 700)        # 大門駅のあたり
    got = [e for lst in g.adj.values() for _, e in lst if e.get("barrier")]
    assert len(got) > 500
    assert set(got[0]["barrier"]) == {"lev_diff", "width", "vtcl_slope", "roof", "route_type"}


def test_a_step_costs_more_for_a_stroller(minato):
    """**段差が大きいほど重い。** ベビーカーは 10cm 超を強く避ける。"""
    prof = profile_mod.for_mode("stroller")
    base = {"length": 100.0, "highway": "footway", "attrs": {},
            "walk": 1, "bike": 1, "car": 0}
    flat = prof.edge_cost(dict(base, barrier={"lev_diff": "1", "width": "4",
                                              "vtcl_slope": "1", "roof": "1",
                                              "route_type": "1"}), "walk")
    big = prof.edge_cost(dict(base, barrier={"lev_diff": "5", "width": "4",
                                             "vtcl_slope": "1", "roof": "1",
                                             "route_type": "1"}), "walk")
    assert big == pytest.approx(flat * 8.0)
    # 屋根があると少し安くなる（雨の日に通りたい道）
    roofed = prof.edge_cost(dict(base, barrier={"lev_diff": "1", "width": "4",
                                                "vtcl_slope": "1", "roof": "2",
                                                "route_type": "1"}), "walk")
    assert roofed < flat


def test_stroller_avoids_steps_more_than_a_plain_walk(minato):
    """**年齢で重みが変わる。** ベビーカーのほうが段差を強く避ける。"""
    base = {"length": 100.0, "highway": "footway", "attrs": {},
            "walk": 1, "bike": 1, "car": 0,
            "barrier": {"lev_diff": "5", "width": "1", "vtcl_slope": "7",
                        "roof": "1", "route_type": "1"}}
    stroller = profile_mod.for_mode("stroller").edge_cost(dict(base), "walk")
    plain = profile_mod.for_mode("walk").edge_cost(dict(base), "walk")
    assert stroller > plain * 2


def test_it_is_never_impassable(minato):
    """**ほこナビでは道を塞がない。**

    当てはめは「中点から 25m 以内」なので歩道 1 本ぶんずれることがある
    （ほこナビが階段と言う 236 本のうち OSM も steps なのは 110 本）。
    誤って塞ぐと経路そのものが出なくなるので、重くするだけにしてある。
    """
    prof = profile_mod.for_mode("stroller")
    worst = {"length": 100.0, "highway": "footway", "attrs": {},
             "walk": 1, "bike": 1, "car": 0,
             "barrier": {"lev_diff": "5", "width": "1", "vtcl_slope": "7",
                         "roof": "1", "route_type": "6"}}
    assert prof.edge_cost(worst, "walk") is not None


def test_the_route_actually_changes(minato):
    """**ここが本題。** 同じ 2 点でも、ほこナビがあると通る道が変わる。"""
    prof = profile_mod.for_mode("stroller")
    a, b = (35.65990, 139.74908), (35.65500, 139.75600)   # 大門駅の西と東
    with_hk = walk.WalkGraph(minato.walk_db_path)
    without = walk.WalkGraph(minato.walk_db_path)
    without.has_barrier = False
    ra = with_hk.route(a, b, prof, "walk")
    rb = without.route(a, b, prof, "walk")
    assert ra and rb
    # 段差を避けるぶん、少し遠回りか時間がかかる（同じになることもあるので or で見る）
    assert ra["seconds"] != rb["seconds"] or ra["meters"] != rb["meters"]
