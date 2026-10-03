"""S27 GTFS-Pathways を乗り換えのコストに入れる。**注記から、駅の選び方そのものへ。**

2026-09-28 まで、`transit.Router` は `indoor` を受け取っていたが**探索では一度も
使っていなかった**（`self.indoor` に入れるだけ）。出入口の案内は経路を出したあとの
注記でしかなかった。

**絶対の所要時間は足さない。** Pathways があるのは大江戸線の 12 駅だけで、
JR や東京メトロの駅には無い。中身が分かっている駅にだけ数分足すと、
**データを持っている駅ほど不利になる**という逆さまのことが起きる。
足すのは「同じ駅での、徒歩との差」だけ。
"""
import pytest

from backend import (gtfs, indoor as indoor_mod, profile as profile_mod,
                     transit, walk)


@pytest.fixture(scope="module")
def router(minato):
    con = gtfs.connect(minato.db_path)
    net = transit.Network(con, minato.transfer_db_path)
    io = indoor_mod.IndoorNetwork(con, "toei_pathways")
    return transit.Router(net, walk.WalkGraph(minato.walk_db_path), io)


def test_inside_the_station_takes_longer_with_a_stroller(minato):
    """**駅の中の所要時間は、ベビーカーだと伸びる。** 階段もエスカレーターも使えないため。"""
    con = gtfs.connect(minato.db_path)
    io = indoor_mod.IndoorNetwork(con, "toei_pathways")
    sid = [k for k, v in io.stations.items() if v["stop_name"] == "大門"][0]
    plain = io.station_seconds(sid, profile_mod.for_mode("walk"))
    stroller = io.station_seconds(sid, profile_mod.for_mode("stroller"))
    assert plain and stroller and stroller > plain
    # 新宿は段差なしでは乗り場に行けない（出入口 3 か所とも）
    sjk = [k for k, v in io.stations.items() if v["stop_name"] == "新宿"][0]
    assert io.station_seconds(sjk, profile_mod.for_mode("stroller")) is None


def test_plain_walk_pays_nothing_extra(router):
    """**基準は「ふつうに歩く人」。** だから徒歩では 1 駅も余計にかからない。"""
    assert router.indoor_extra(profile_mod.for_mode("walk")) == {}


def test_only_the_difference_is_charged(router):
    """足すのは徒歩との差だけ。大門は +105 秒くらいで、構内の 414 秒まるごとではない。"""
    extra = router.indoor_extra(profile_mod.for_mode("stroller"))
    assert len(extra) == 12
    daimon = [v for k, v in extra.items() if k.startswith("大門")]
    assert daimon and 60 < daimon[0] < 200


def test_it_does_not_leak_to_other_companies_stations(router):
    """**新宿は 400m 以内に JR の駅もある。**

    名前と距離だけで対応づけると、大江戸線の構内の時間を JR 新宿駅にも付けてしまう。
    Pathways が言っているのは都営地下鉄の構内のことだけなので、フィードで絞る。
    """
    extra = router.indoor_extra(profile_mod.for_mode("stroller"))
    shinjuku = {k for k in extra if k.startswith("新宿")}
    assert shinjuku                      # 都営の新宿・新宿西口には付く
    feeds = {f for k in shinjuku for f, _ in router.net.places[k]["members"]}
    assert feeds == {"toei_train"}       # JR の新宿には付かない


def test_a_station_that_cannot_be_reached_is_not_blocked(router, minato):
    """**「この駅は行けない」とはしない。**

    大江戸線の管理範囲しかデータが無く、実際には他社のエレベーターで
    つながっていることがある。通れなくするのではなく、重く見積もるだけ。
    """
    extra = router.indoor_extra(profile_mod.for_mode("stroller"))
    shinjuku = [v for k, v in extra.items() if k == "新宿#0"]
    assert shinjuku and shinjuku[0] > 0          # 重くはなるが
    assert "新宿#0" in router.net.places          # 場所としては残っている


def test_access_includes_the_indoor_time(router):
    """徒歩の脚に、駅の中の時間が乗っていること。"""
    home = (35.6565, 139.7553)                   # 大門のすぐそば
    plain = router.access(home, profile_mod.for_mode("walk"))
    stroller = router.access(home, profile_mod.for_mode("stroller"))
    daimon = [p for p in plain if p.startswith("大門")]
    assert daimon
    pid = daimon[0]
    assert pid in stroller
    # ベビーカーは歩くのも遅いが、それだけでは説明が付かないほど差が出る
    assert stroller[pid] > plain[pid] + 60


def test_the_transit_route_changes(client):
    """**ここが本題。** ベビーカーだと、通る駅そのものが変わりうる。"""
    body = {"stage": "minato", "from_lat": 35.6371, "from_lon": 139.7501,
            "from_name": "自宅", "to_lat": 35.66096, "to_lon": 139.73077,
            "to_name": "六本木区民協働スペース", "date": "2026-09-24",
            "time": "10:00", "modes": ["train"]}
    a = client.post("/api/routes", json=dict(body, mode="walk")).get_json()["train"]
    b = client.post("/api/routes", json=dict(body, mode="stroller")).get_json()["train"]
    assert a and b
    def stations(r):
        return [l.get("from") for l in r["legs"] if l["mode"] == "transit"]
    assert stations(a) != stations(b) or a["arrive"] != b["arrive"]
