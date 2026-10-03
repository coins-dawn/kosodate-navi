"""S30 当て馬の道（段差をよけない、ふつうの最短）を並べて出す。

よけた道を 1 本だけ出しても、**何をよけたのかが画面に出ない**。同じ区間を
よけない道でも引いて並べ、そちらに階段や段差の印を置くと、遠回りの代わりに
何を通らずに済んだのかが分かる。
"""
from datetime import date

from backend import plan as plan_mod
from backend import profile, transit, walk

ON = date(2026, 9, 24)      # 木曜
HOME = (35.8700, 139.9220, "自宅")
DEST = (35.8761, 139.9318, "十太夫児童センター")


def planner(stage, con):
    net = transit.Network(con, stage.transfer_db_path)
    wg = walk.WalkGraph(stage.walk_db_path)
    return plan_mod.Planner(stage, transit.Router(net, wg), wg)


def compares(results, p):
    return [l.get("compare") for r in p.shown(results) for l in r["legs"]
            if l["mode"] == "walk" and l.get("compare")]


# --- 分かれ目の見つけ方（データ不要） ---

def test_diverge_finds_the_middle():
    """頭とお尻が同じなら、**違うところだけ**を返す。"""
    chosen = [[0, 0], [1, 0], [1, 1], [2, 1], [3, 0]]
    plain = [[0, 0], [1, 0], [2, 0], [3, 0]]
    assert plan_mod.diverge(chosen, plain) == (2, 3)


def test_diverge_returns_none_for_the_same_line():
    line = [[0, 0], [1, 0], [2, 0]]
    assert plan_mod.diverge(line, list(line)) is None


def test_diverge_returns_none_when_one_contains_the_other():
    """片方がもう片方の頭の部分と同じなら、比べるものが無い。"""
    assert plan_mod.diverge([[0, 0], [1, 0], [2, 0]], [[0, 0], [1, 0]]) is None


# --- 当て馬の profile ---

def test_plain_profile_lets_you_use_stairs():
    """ベビーカーでは通れない階段が、当て馬では通れる（重みも 1.0）。"""
    stroller = profile.for_mode("stroller")
    steps = {"highway": "steps", "attrs": {}, "length": 10.0, "walk": 1, "bike": 0, "car": 0}
    assert stroller.edge_cost(steps) is None
    plain = stroller.plain()
    assert plain.edge_cost(steps) is not None
    flat = {"highway": "footway", "attrs": {}, "length": 70.0, "walk": 1, "bike": 0, "car": 0}
    # 重みが全部 1.0 なので、素の「距離 ÷ 速さ」になる
    assert abs(plain.edge_cost(flat) - 70.0 / plain.walk_speed * 60) < 1e-9


def test_plain_profile_ignores_hokonavi():
    """ほこナビの段差も、当て馬では重くしない（よけないのが当て馬の役目）。"""
    stroller = profile.for_mode("stroller")
    edge = {"highway": "footway", "attrs": {}, "length": 50.0, "walk": 1, "bike": 0, "car": 0,
            "barrier": {"lev_diff": "5", "width": "1", "vtcl_slope": "6", "roof": "1"}}
    plain = stroller.plain()
    assert stroller.edge_cost(edge) > plain.edge_cost(edge) * 5
    assert abs(plain.edge_cost(edge) - 50.0 / plain.walk_speed * 60) < 1e-9


def test_plain_profile_keeps_the_walking_speed():
    """速さまで変えると「よけた道が遠い」のか「歩くのが遅い」のか分からなくなる。"""
    stroller = profile.for_mode("stroller")
    assert stroller.plain().walk_speed == stroller.walk_speed


# --- 経路に載ること ---

def test_stroller_walk_leg_gets_a_comparison(nagareyama, nagareyama_con):
    """**自宅 → 駅で、階段を通らずに済んでいることが数字で出る。**"""
    p = planner(nagareyama, nagareyama_con)
    results = p.transit_plans(HOME, DEST, 10 * 3600, profile.for_mode("stroller"), on_date=ON)
    got = compares(results, p)
    assert got, "当て馬の道が付いていない"
    for c in got:
        assert c["obstacles"]
        assert all(o["kind"] in ("steps", "step", "narrow", "steep") for o in c["obstacles"])
        assert c["extra_m"] >= 0
        assert len(c["coords"]) >= 2


def test_the_chosen_way_really_avoids_what_the_comparison_shows(nagareyama, nagareyama_con):
    """**当て馬にあるものが、えらんだ道には無いこと。** ここが崩れると嘘になる。"""
    p = planner(nagareyama, nagareyama_con)
    results = p.transit_plans(HOME, DEST, 10 * 3600, profile.for_mode("stroller"), on_date=ON)
    legs = [l for r in p.shown(results) for l in r["legs"]
            if l["mode"] == "walk" and l.get("compare")]
    assert legs
    for l in legs:
        assert l["steps"] == 0 and l["step_free"]
        assert any(o["kind"] == "steps" for o in l["compare"]["obstacles"])


def test_no_comparison_without_a_stroller(nagareyama, nagareyama_con):
    """ふつうの歩きでは出さない（よけていないので、線が 2 本になるだけ）。"""
    p = planner(nagareyama, nagareyama_con)
    results = p.transit_plans(HOME, DEST, 10 * 3600, profile.for_mode("walk"), on_date=ON)
    assert not compares(results, p)


def test_comparison_line_starts_and_ends_on_the_chosen_way(nagareyama, nagareyama_con):
    """**分かれてから合流するまで**だけを渡す（同じ道を 2 本重ねない）。"""
    p = planner(nagareyama, nagareyama_con)
    results = p.transit_plans(HOME, DEST, 10 * 3600, profile.for_mode("stroller"), on_date=ON)
    legs = [l for r in p.shown(results) for l in r["legs"]
            if l["mode"] == "walk" and l.get("compare")]
    assert legs
    for l in legs:
        c = l["compare"]
        assert len(c["coords"]) < len(l["coords"]) + 2
        for end in (c["coords"][0], c["coords"][-1]):
            near = min(transit.haversine(end[1], end[0], q[1], q[0]) for q in l["coords"])
            assert near < 1, "当て馬の線の端が、えらんだ道から離れている"
