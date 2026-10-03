"""S6 駅の中（GTFS-Pathways）。

**いまの舞台（流山市）に構内データは無い。** GTFS-Pathways を出しているのは
都営地下鉄など一部の事業者だけなので、`indoor: null` の舞台で画面が壊れないことを見る。
読み取りと「段差なしで入れる出入口」の判定は残してあるので、
`indoor.feed` を持つ舞台を足せばそのまま効く。
"""
from backend import indoor


def test_absent_indoor_is_harmless():
    net = indoor.IndoorNetwork(None)
    assert net.available is False
    assert net.summary()["stations"] == 0
    assert net.summary()["entrances"] == 0


def test_stage_without_pathways_reports_unavailable(nagareyama, nagareyama_con):
    feed = (nagareyama.indoor or {}).get("feed") if nagareyama.indoor else None
    assert feed is None
    net = indoor.IndoorNetwork(nagareyama_con, feed)
    assert net.available is False
