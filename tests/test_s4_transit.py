"""S4 時刻表探索。徒歩の脚は歩行グラフから作る。"""
from datetime import date

from backend import profile, transit, walk

ON = date(2026, 9, 24)      # 木曜


def router(stage, con):
    net = transit.Network(con, stage.transfer_db_path)
    return transit.Router(net, walk.WalkGraph(stage.walk_db_path)), net


def test_transfers_are_precomputed(nagareyama, nagareyama_con):
    _, net = router(nagareyama, nagareyama_con)
    assert net.transfers("stroller")
    assert net.transfers("normal")


def test_route_uses_tx(nagareyama, nagareyama_con):
    r, _ = router(nagareyama, nagareyama_con)
    res = r.search_many((35.8562, 139.9029, "自宅"), (35.8710, 139.9260, "流山おおたかの森"),
                        9 * 3600, profile.for_months(96), on_date=ON)
    assert res
    names = [l.get("route_name") for x in res for l in x["legs"] if l["mode"] == "transit"]
    assert any("つくばエクスプレス" in (n or "") for n in names)


def test_blank_wheelchair_flag_is_not_excluded(nagareyama, nagareyama_con):
    """流山ぐりーんバスは全便 wheelchair_accessible が空欄。

    空欄を「乗れない」と扱うと 1 本も出なくなる（データを出していない事業者が不利になる）。
    """
    r, net = router(nagareyama, nagareyama_con)
    blanks = [t for t in net.trips.values()
              if t["feed_id"] == "greenbus" and not (t["wheelchair_accessible"] or "")]
    assert len(blanks) == 338
    p = profile.for_months(6)
    assert p.stroller is True
    # ベビーカーでも、空欄の便は候補から外れない
    active = net.active_trips(ON)
    usable = [k for k in active if k[0] == "greenbus"]
    assert usable


def test_access_uses_walk_graph(nagareyama, nagareyama_con):
    r, _ = router(nagareyama, nagareyama_con)
    got = r.access((35.8700, 139.9220), profile.for_months(96))
    assert got, "おおたかの森の周りに駅と停留所が見つかるはず"
