"""S3 徒歩・自転車グラフ。年齢で通れる道が変わることを確かめる。"""
from backend import profile, walk


def test_graph_available(nagareyama):
    assert walk.WalkGraph(nagareyama.walk_db_path).available


def test_stroller_avoids_steps(nagareyama):
    g = walk.WalkGraph(nagareyama.walk_db_path)
    origin, dest = (35.8700, 139.9220), (35.8562, 139.9029)  # おおたかの森 → 市役所
    baby = g.route(origin, dest, profile.for_months(6), "walk")
    school = g.route(origin, dest, profile.for_months(96), "walk")
    assert baby is not None and school is not None
    assert baby["steps"] == 0
    assert baby["step_free"] is True


def test_steps_blocked_in_profile():
    assert profile.for_months(6).band["steps"] is None      # ベビーカーは階段を通れない
    assert profile.for_months(96).band["steps"] is not None


def test_bike_is_faster_than_walk(nagareyama):
    g = walk.WalkGraph(nagareyama.walk_db_path)
    p = profile.for_months(96)
    o, d = (35.8562, 139.9029), (35.8397, 139.9036)
    assert g.route(o, d, p, "bike")["seconds"] < g.route(o, d, p, "walk")["seconds"]
