"""S19 地図に描く経路の線を、実際の道の形にする。

バスは GTFS の `shapes.txt`、鉄道は OpenStreetMap の線路（つくばエクスプレスの
GTFS には shapes.txt が無いため）。停留所を直線で結ぶのは最後の手段。

**歩く区間も同じ。** 出発地から駅まで、駅から行き先までを 2 点で結ぶと、
建物や川を突き抜けた直線になる。歩行グラフ（OSM＋ほこナビ）の道なりに引き直す。
"""
from datetime import date

from backend import plan as plan_mod
from backend import profile, railshape, transit, walk

ON = date(2026, 9, 24)      # 木曜
HOME = (35.8700, 139.9220, "自宅")


def planner(stage, con):
    net = transit.Network(con, stage.transfer_db_path)
    wg = walk.WalkGraph(stage.walk_db_path)
    return plan_mod.Planner(stage, transit.Router(net, wg), wg), net


def transit_legs(result):
    return [l for l in result["legs"] if l["mode"] == "transit"]


# --- 線の切り出し（ネットワーク不要の部分） ---

def test_snap_to_line_finds_the_nearest_segment():
    pts = [(139.90, 35.80), (139.91, 35.80), (139.92, 35.80)]
    d = transit.snap_to_line(pts, 139.915, 35.801)
    assert len(d) == 2
    near = min(range(len(d)), key=lambda i: d[i][0])
    assert near == 1
    assert d[near][0] < 150                      # 緯度 0.001 度 ≒ 111m


def test_best_pair_keeps_boarding_before_alighting():
    """**周回路線で壊れないこと。**

    始発と終点が同じ停留所だと、乗る停留所は形状の初めにも終わりにも当たる。
    いちばん近い線分をそれぞれ選ぶと、乗る側が終わりに当たって区間が消える。
    """
    da = [0.0, 50.0, 80.0, 5.0]                  # 乗る側は線分 0 と 3 の両方に近い
    db = [90.0, 4.0, 60.0, 70.0]                 # 降りる側は線分 1
    i, j = transit.best_pair([(x, None) for x in da], [(x, None) for x in db])
    assert i == 0 and j == 1


def test_rail_shapes_cover_tsukuba_express(nagareyama):
    rs = railshape.RailShapes(nagareyama)
    assert rs.available
    assert "つくばエクスプレス" in rs.names()


def test_rail_shape_between_two_stations_follows_the_track(nagareyama):
    rs = railshape.RailShapes(nagareyama)
    otaka, minami = (139.9251, 35.8718), (139.9035, 35.8387)
    coords = rs.between(otaka, minami, name="つくばエクスプレス")
    assert coords and len(coords) > 20            # 直線なら 2 点で済む
    straight = transit.haversine(otaka[1], otaka[0], minami[1], minami[0])
    length = transit.line_length(coords)
    assert straight < length < straight * 1.5     # 線路なので少し長い、が遠回りではない
    assert transit.haversine(coords[0][1], coords[0][0], otaka[1], otaka[0]) < 400
    assert transit.haversine(coords[-1][1], coords[-1][0], minami[1], minami[0]) < 400


def test_rail_shape_matches_by_operator_when_the_name_differs(nagareyama):
    """**時刻表と OSM で路線名が揃わないことがある。**

    GTFS の「東武アーバンパークライン」は、OSM では「東武野田線」。
    名前で当たらないときは運行者（東武鉄道）で絞る。
    """
    rs = railshape.RailShapes(nagareyama)
    assert "東武アーバンパークライン" not in rs.names()
    assert "東武野田線" in rs.names()
    otaka, edogawadai = (139.9258, 35.8721), (139.9105, 35.8972)
    coords = rs.between(otaka, edogawadai,
                        name="東武アーバンパークライン", operator="東武鉄道")
    assert coords and len(coords) > 20
    straight = transit.haversine(otaka[1], otaka[0], edogawadai[1], edogawadai[0])
    assert straight < transit.line_length(coords) < straight * 1.5


# --- 経路に載ること ---

def test_bus_leg_uses_gtfs_shapes(nagareyama, nagareyama_con):
    """流山ぐりーんバスは shapes.txt を出している（23 形状・4,046 点）。"""
    p, _ = planner(nagareyama, nagareyama_con)
    results = p.transit_plans(HOME, (35.8761, 139.9318, "十太夫児童センター"),
                              10 * 3600, profile.for_months(24), on_date=ON)
    legs = [l for r in results for l in transit_legs(r) if l["shape"] == "gtfs"]
    assert legs, "バスの脚が GTFS の形状になっていない"
    for l in legs:
        assert len(l["coords"]) > 2               # 停留所を結んだだけなら点は停留所の数
        straight = transit.haversine(l["from_lat"], l["from_lon"], l["to_lat"], l["to_lon"])
        assert transit.line_length(l["coords"]) >= straight * 0.9


def test_train_leg_falls_back_to_osm(nagareyama, nagareyama_con):
    """つくばエクスプレスの GTFS には shapes.txt が無いので、線路（OSM）で描く。"""
    p, _ = planner(nagareyama, nagareyama_con)
    results = p.transit_plans(HOME, (35.8455, 139.9027, "南流山あたり"),
                              10 * 3600, profile.for_months(96), on_date=ON)
    legs = [l for r in results for l in transit_legs(r)
            if "つくばエクスプレス" in (l.get("route_name") or "")]
    assert legs
    assert all(l["shape"] == "osm" for l in legs)
    assert all(len(l["coords"]) > 10 for l in legs)


def test_tobu_leg_is_drawn_from_osm(nagareyama, nagareyama_con):
    """東武アーバンパークラインも shapes.txt を持たない（2026-09-25 に取り込み）。"""
    p, _ = planner(nagareyama, nagareyama_con)
    results = p.transit_plans(HOME, (35.8975, 139.9110, "江戸川台のそば"),
                              10 * 3600, profile.for_months(96), on_date=ON)
    legs = [l for r in results for l in transit_legs(r)
            if (l.get("operator") or "") == "東武鉄道"]
    assert legs
    assert all(l["shape"] == "osm" and len(l["coords"]) > 10 for l in legs)


def test_tx_gtfs_really_has_no_shapes(nagareyama_con):
    """**前提そのもののテスト。** 配信側が shapes.txt を出し始めたら、ここが落ちて
    気づける（そのときは OSM ではなく GTFS を使うほうが正しい）。"""
    n = nagareyama_con.execute(
        "SELECT COUNT(*) c FROM shape_point WHERE feed_id='mir_train'").fetchone()["c"]
    assert n == 0
    t = nagareyama_con.execute(
        "SELECT COUNT(*) c FROM shape_point WHERE feed_id='tobu'").fetchone()["c"]
    assert t == 0                                # 東武鉄道の GTFS にも shapes.txt は無い
    m = nagareyama_con.execute(
        "SELECT COUNT(*) c FROM shape_point WHERE feed_id='greenbus'").fetchone()["c"]
    assert m > 1000


def test_shape_source_is_always_recorded(nagareyama, nagareyama_con):
    """どの脚にも出どころが残ること（gtfs / osm / stops）。"""
    p, _ = planner(nagareyama, nagareyama_con)
    results = p.transit_plans(HOME, (35.8455, 139.9027, "南流山あたり"),
                              10 * 3600, profile.for_months(24), on_date=ON)
    legs = [l for r in results for l in transit_legs(r)]
    assert legs
    assert all(l.get("shape") in ("gtfs", "osm", "stops") for l in legs)


# --- 歩く区間 ---

def walk_legs(result):
    return [l for l in result["legs"] if l["mode"] == "walk"]


def test_walk_leg_follows_the_footpath(nagareyama, nagareyama_con):
    """**出発地 → 駅の徒歩が直線になっていないこと。**

    自宅（流山おおたかの森の住宅地）から駅までは 500m ほどあり、道なりなら
    曲がり角のぶんだけ点が増え、直線距離より長くなる。
    """
    p, _ = planner(nagareyama, nagareyama_con)
    results = p.transit_plans(HOME, (35.8761, 139.9318, "十太夫児童センター"),
                              10 * 3600, profile.for_months(24), on_date=ON)
    legs = [l for r in p.shown(results) for l in walk_legs(r)]
    assert legs
    far = [l for l in legs
           if transit.haversine(l["from_lat"], l["from_lon"], l["to_lat"], l["to_lon"]) > 200]
    assert far, "200m 以上歩く脚が無く、直線かどうかを試せていない"
    for l in far:
        assert l["shape"] == "osm"
        assert len(l["coords"]) > 2               # 直線なら 2 点で済む
        straight = transit.haversine(l["from_lat"], l["from_lon"], l["to_lat"], l["to_lon"])
        assert transit.line_length(l["coords"]) >= straight
        assert transit.haversine(l["coords"][0][1], l["coords"][0][0],
                                 l["from_lat"], l["from_lon"]) < 1
        assert transit.haversine(l["coords"][-1][1], l["coords"][-1][0],
                                 l["to_lat"], l["to_lon"]) < 1


def test_walk_shape_agrees_with_the_time_it_reports(nagareyama, nagareyama_con):
    """**線と時間が食い違わないこと。**

    所要時間は探索（`walk.reachable_points`）が測ったもので、線はあとから
    引き直したもの。**別々に出しているので、ずれていないかを確かめる。**
    同じグラフ・同じ条件なら「描いた線の長さ ÷ 時間」はその年齢の歩く速さに収まる。

    短い脚は外す。道に出るまでの straight な接続（グラフのノードは道の上にしかない）が
    占める割合が大きく、時間のほうはその分を一部しか数えていない。
    """
    p, _ = planner(nagareyama, nagareyama_con)
    checked = 0
    for months in (0, 24, 96):
        prof = profile.for_months(months)
        results = p.transit_plans(HOME, (35.8761, 139.9318, "十太夫児童センター"),
                                  10 * 3600, prof, on_date=ON)
        for r in p.shown(results):
            for l in walk_legs(r):
                if l.get("shape") != "osm" or l["meters"] < 200:
                    continue
                speed = transit.line_length(l["coords"]) / (l["seconds"] / 60)   # m/分
                assert 0.85 < speed / prof.walk_speed < 1.15, \
                    f"{l['from']}→{l['to']} の線が時間に合わない（{speed:.0f}m/分）"
                checked += 1
    assert checked


def test_stroller_walk_shape_avoids_steps(nagareyama, nagareyama_con):
    """ベビーカーの線は階段を通らない（時間を出したときと同じ条件で引く）。"""
    p, _ = planner(nagareyama, nagareyama_con)
    results = p.transit_plans(HOME, (35.8455, 139.9027, "南流山あたり"),
                              10 * 3600, profile.for_months(0), on_date=ON)
    legs = [l for r in p.shown(results) for l in walk_legs(r) if l.get("shape") == "osm"]
    assert legs
    assert all(l["steps"] == 0 and l["step_free"] for l in legs)


def test_walk_shape_is_reused_across_plans(nagareyama, nagareyama_con):
    """**同じ脚を何度も歩き直さないこと。**

    5 本の候補は同じ駅まで歩くので、出発地側の脚は使い回せる。
    引いた本数がキャッシュの数を超えないことで確かめる。
    """
    p, _ = planner(nagareyama, nagareyama_con)
    dest = (35.8761, 139.9318, "十太夫児童センター")
    p.transit_plans(HOME, dest, 10 * 3600, profile.for_months(24), on_date=ON)
    first = len(p._walk_shapes)
    assert first
    p.transit_plans(HOME, dest, 10 * 3600, profile.for_months(24), on_date=ON)
    assert len(p._walk_shapes) == first          # 2 回目は 1 本も引き直していない
