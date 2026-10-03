"""S2 GTFS の取り込み。件数は実データと突き合わせる。"""


def counts(con, feed_id):
    stops = con.execute("SELECT COUNT(*) FROM stop WHERE feed_id=?", (feed_id,)).fetchone()[0]
    trips = con.execute("SELECT COUNT(*) FROM trip WHERE feed_id=?", (feed_id,)).fetchone()[0]
    return stops, trips


def test_tx_feed(nagareyama_con):
    assert counts(nagareyama_con, "mir_train") == (20, 841)


def test_greenbus_feed(nagareyama_con):
    stops, trips = counts(nagareyama_con, "greenbus")
    assert stops == 181 and trips == 338
    routes = nagareyama_con.execute(
        "SELECT COUNT(*) FROM route WHERE feed_id='greenbus'").fetchone()[0]
    assert routes == 6


def test_challenge_only_feed_is_skipped(nagareyama):
    # 東武鉄道は api-challenge 側。トークンが無くても舞台は成立する
    tobu = [t for t in nagareyama.transit if t["id"] == "tobu"][0]
    assert tobu["required"] is False
