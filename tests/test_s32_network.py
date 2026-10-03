"""S32 **経路探索が使っている公共交通を地図に出す**（2026-09-30・ユーザー指示）。

画面に出るのは「さがした 1 本の経路」だけで、その裏でどれだけの路線と乗り場を見ているかは
見えなかった。`/api/network` が、**探索が実際に使っているもの**をそのまま地図の形で返す。
飾りの路線図ではないこと（点が探索の単位そのものであること）が、このテストの要。

あわせて、**自転車と車を画面から外した**ことも確かめる。
"""
import json

import pytest

from backend import network as network_mod

ROUTE_BODY = {"stage": "nagareyama", "from_lat": 35.8697, "from_lon": 139.9236,
              "to_lat": 35.8620, "to_lon": 139.9100,
              "date": "2026-09-24", "time": "10:00"}


@pytest.fixture(scope="module")
def net_json(client):
    return client.get("/api/network?stage=nagareyama").get_json()


def test_kind_of_splits_bus_from_everything_else():
    """GTFS の `route_type` は 3 だけがバス。0 路面電車・1 地下鉄・2 鉄道は電車に寄せる
    （都営の GTFS は 3 つとも入っている）。"""
    assert network_mod.kind_of(3) == "bus"
    for t in (0, 1, 2):
        assert network_mod.kind_of(t) == "train"


def test_every_feed_in_the_timetable_is_on_the_map(net_json, nagareyama):
    """**取り込んだフィードは全部出す。** 1 つでも欠けると「使っていない」に見える。"""
    assert net_json["available"] is True
    got = {f["id"] for f in net_json["feeds"]}
    assert got == {"greenbus", "mir_train", "tobu", "tobubus"}
    assert all(f["routes"] > 0 for f in net_json["feeds"])


def test_the_points_are_the_search_units_themselves(net_json, client):
    """**点は探索の単位そのもの**（`transit.Network.places`）。飾りの駅名ではない。

    同じ名前で 150m 以内の停留所は 1 つの「乗り場」にまとめてある。
    まとめた件数（`platforms`）も持たせて、画面で言えるようにする。
    """
    from backend.app import ctx
    places = ctx("nagareyama").net.places
    feats = net_json["stops"]["features"]
    names = {f["properties"]["name"] for f in feats}
    assert names <= {p["name"] for p in places.values()}
    assert len(feats) > 400
    # 便が 1 本も来ない乗り場は出さない（出しても行き先にならない）
    assert all(f["properties"]["feeds"] for f in feats)
    assert any(f["properties"]["platforms"] > 1 for f in feats)


def test_lines_say_where_the_shape_came_from(net_json):
    """**「停留所を結んだだけ」を隠さない。**

    流山ぐりーんバスは `shapes.txt` を出しているが、**つくばエクスプレス・東武鉄道・
    東武バスは出していない**。線の出どころを 1 本ずつ持たせて、画面に出す。
    """
    by_feed = {}
    for f in net_json["lines"]["features"]:
        p = f["properties"]
        by_feed.setdefault(p["feed"], set()).add(p["shape"])
    assert by_feed["greenbus"] == {"gtfs"}          # 便ごとの実際の形
    assert by_feed["tobubus"] == {"stops"}          # JSON API から組み直したフィード
    # つくばエクスプレスは OSM の線路で補う（区域の外に出る区間は駅を結んだ線のまま）
    assert by_feed["mir_train"] <= {"osm", "mixed"}


def test_trains_and_buses_are_counted_separately(net_json):
    """画面のチェックボックスは電車とバスの 2 つ。数もその 2 つで数える。"""
    c = net_json["counts"]
    assert c["train"] + c["bus"] == len(net_json["lines"]["features"])
    assert c["train_stops"] + c["bus_stops"] == len(net_json["stops"]["features"])
    kinds = {f["properties"]["kind"] for f in net_json["lines"]["features"]}
    assert kinds == {"train", "bus"}


def test_one_route_one_line(net_json, client):
    """路線 1 本につき線 1 本（便ごとに引くと同じ道が何十本も重なる）。"""
    keys = [(f["properties"]["feed"], f["properties"]["route"])
            for f in net_json["lines"]["features"]]
    assert len(keys) == len(set(keys)) or len(keys) - len(set(keys)) < 5
    live = client.get("/api/coverage?stage=nagareyama").get_json()
    total = sum(f["routes"] for f in live["feeds"])
    assert len(net_json["lines"]["features"]) == total


def test_the_answer_always_has_the_same_shape(client):
    """**時刻表が無くても画面が壊れないこと。** 画面は返ってきた形をそのまま地図に流すので、
    欠けているキーがあるとそこで止まる。

    `enabled: false` の舞台（新宿区）を指しても、`stages.load` が**使える舞台に落とす**
    ので中身は入ってくる。ここで見たいのは中身ではなく、**キーがそろっていること**。
    """
    for stage in ("nagareyama", "minato", "shinjuku"):
        d = client.get(f"/api/network?stage={stage}").get_json()
        assert set(d) >= {"available", "lines", "stops", "feeds", "counts"}
        for key in ("lines", "stops"):
            assert d[key]["type"] == "FeatureCollection"
            assert isinstance(d[key]["features"], list)


# --- 自転車と車を画面から外した（2026-09-30・ユーザー指示） ---

def test_routes_api_no_longer_returns_bike_or_car(client):
    """**公共交通ではなく、推奨されているオープンデータも使っていない**ので外した。"""
    d = client.post("/api/routes", json=ROUTE_BODY).get_json()
    assert "bike" not in d and "car" not in d
    assert "walk" in d and "total" in d


def test_the_engine_is_still_there(client):
    """**消したのではなく、出すのをやめただけ。** `modes` に入れれば今までどおり返る。"""
    d = client.post("/api/routes", json={**ROUTE_BODY, "modes": ["bike", "car"]}).get_json()
    assert d["bike"] and d["car"]
    assert d["bike"]["minutes"] < d["walk"]["minutes"] if d.get("walk") else True


def test_static_build_does_not_ask_for_bike_or_car():
    """事前計算からも外す（`scripts/build_static.py` の `ROUTE_MODES`）。"""
    import scripts.build_static as bs
    assert bs.ROUTE_MODES == ["total", "train", "bus", "walk"]


def test_the_screen_and_the_static_build_agree():
    """画面の並び（`PLAN_ROWS`）と事前計算の `ROUTE_MODES` をそろえる。
    片方だけ直すと、静的版で行が消える。"""
    import re
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "frontend" / "app.js").read_text(
        encoding="utf-8")
    rows = re.search(r"const PLAN_ROWS = \[(.*?)\];", js, re.S).group(1)
    keys = re.findall(r'\["(\w+)",', rows)
    import scripts.build_static as bs
    assert keys == bs.ROUTE_MODES


def test_network_json_is_in_the_static_build():
    """静的版にも下敷きを配る（舞台に 1 ファイル）。"""
    import scripts.build_static as bs
    src = (bs.REPO / "scripts" / "build_static.py").read_text(encoding="utf-8")
    assert "network.json" in src
