"""S21 バスの JSON API（東武バス）を GTFS に組み直して取り込む。

JSON には**停留所の座標が無い**ので、名前で照合して補っている。
ここでは「補い方の規則」と「取り込んだ結果」を確かめる（ネットワークは使わない）。
"""
import json

from backend import odptbus

REPORT_KEYS = {"patterns", "trips", "stops", "stops_without_location",
               "stops_from_gtfs", "stops_from_osm"}


# --- 名前の正規化（照合の要） ---

def test_stop_name_is_normalized_for_matching():
    n = odptbus.norm_stop_name
    assert n("ショッピングセンター前（流山市）") == n("ショッピングセンター前")
    assert n("松ヶ丘") == n("松ケ丘")
    assert n("ＡＬＦＡＬＩＮＫ流山８") == "ALFALINK流山8"
    assert n(" 南流山駅 ") == "南流山駅"


def test_geo_prefers_official_coordinates_and_the_right_cluster():
    """**同じ名前の停留所が離れてあるとき**は、かたまりでまとめて選ぶ。

    「向原」は 17km 離れて 5 つある。事業者名が付いているほう、次に点の多いほうを採る。
    """
    geo = odptbus.Geo()
    geo.add("向原", 35.870, 139.920, "osm:東武バスセントラル")
    geo.add("向原", 35.8701, 139.9201, "osm:")
    geo.add("向原", 36.020, 139.700, "osm:")
    geo.add("向原", 36.0201, 139.7001, "osm:")
    geo.add("向原", 36.0202, 139.7002, "osm:")
    lat, lon, source = geo.find("向原", "東武バス")
    assert abs(lat - 35.870) < 0.001 and source == "osm"
    geo2 = odptbus.Geo()
    geo2.add("おおたかの森病院前", 35.8721, 139.9262, "gtfs")
    geo2.add("おおたかの森病院前", 35.8722, 139.9263, "osm:")
    assert geo2.find("おおたかの森病院前")[2] == "gtfs"


def test_times_after_midnight_go_past_24h():
    assert odptbus.fmt_sec(odptbus.hhmm_to_sec("25:10")) == "25:10:00"
    assert odptbus.hhmm_to_sec("00:05") < odptbus.hhmm_to_sec("23:55")


def test_calendar_mapping_covers_what_odpt_sends():
    for cal in ("odpt.Calendar:Weekday", "odpt.Calendar:Saturday", "odpt.Calendar:Holiday"):
        assert cal in odptbus.CALENDARS
    assert odptbus.CALENDARS["odpt.Calendar:Weekday"] == (1, 1, 1, 1, 1, 0, 0)


# --- 取り込んだ結果 ---

def test_tobubus_is_in_the_database(nagareyama_con):
    row = nagareyama_con.execute(
        "SELECT COUNT(*) c FROM stop WHERE feed_id='tobubus'").fetchone()
    assert row["c"] > 300
    trips = nagareyama_con.execute(
        "SELECT COUNT(*) c FROM trip WHERE feed_id='tobubus'").fetchone()["c"]
    assert trips > 1000
    # 組み直した停留所にも必ず座標がある（無いものは出さない約束）
    bad = nagareyama_con.execute(
        "SELECT COUNT(*) c FROM stop WHERE feed_id='tobubus' "
        "AND (lat IS NULL OR lon IS NULL)").fetchone()["c"]
    assert bad == 0


def test_tobubus_serves_nagareyama(nagareyama, nagareyama_con):
    inside = [r for r in nagareyama_con.execute(
        "SELECT lat, lon FROM stop WHERE feed_id='tobubus'")
        if nagareyama.contains(r["lat"], r["lon"])]
    assert len(inside) > 50, "流山市の中に東武バスの停留所が無い"


def test_build_report_is_kept_next_to_the_zip(nagareyama):
    """**どう補ったかを残す。** 画面の「使えるデータ」がこれを読んで出す。"""
    path = nagareyama.raw_dir / "gtfs" / "tobubus.report.json"
    assert path.exists()
    report = json.loads(path.read_text(encoding="utf-8"))
    assert REPORT_KEYS <= set(report)
    assert report["stops_from_gtfs"] > 0 and report["stops_from_osm"] > 0
    assert report["stops"] > report["stops_without_location"]


def test_coverage_tells_how_the_positions_were_filled(client):
    d = client.get("/api/coverage?stage=nagareyama").get_json()
    built = [f for f in d["feeds"] if f.get("built")]
    assert built, "組み直したフィードの内訳が API に出ていない"
    assert built[0]["built"]["stops_without_location"] >= 0


# --- 名前で当てた座標が別の市に飛ぶ問題（2026-09-30・ユーザー指摘） ---

def test_is_spike_sees_a_detour_not_a_long_ride():
    """**長い区間と、飛んでいる点を取り違えない。**

    高速バスのように 1 区間が長い系統もある。まっすぐ行っても長い区間は
    「寄り道の比」が小さいので引っかからない。
    """
    a, b = (35.870, 139.920), (35.900, 139.920)      # まっすぐ 3.3km
    on_the_way = (35.885, 139.920)                   # あいだにある
    far_away = (35.880, 139.810)                     # 10km 横に飛んでいる
    assert not odptbus.is_spike(a, on_the_way, b)
    assert odptbus.is_spike(a, far_away, b)
    # 200m の寄り道は見逃す（普通の遠回り）
    assert not odptbus.is_spike((35.870, 139.920), (35.8705, 139.9225), (35.871, 139.920))


def test_repair_moves_a_stop_that_landed_in_another_city():
    """**「保健センター前」は草加市にもある。**

    事業者名（東武バスセントラル）は草加市の同名停留所にも付いているので、
    名前と事業者だけでは選び違える。**系統の並びで見ると 11km 飛んでいる**ので気づける。
    """
    geo = odptbus.Geo()
    for lat, lon in ((35.8724, 139.9245), (35.8787, 139.9156), (35.8788, 139.9190)):
        geo.add("となりの停留所%s" % lon, lat, lon, "osm:東武バスセントラル")
    geo.add("保健センター前", 35.8274, 139.8107, "osm:東武バスセントラル")   # 草加市（誤り）
    geo.add("保健センター前", 35.8796, 139.9181, "osm:流山市コミュニティバス")  # 流山市（正解）
    stops = {
        "a": {"name": "となりの停留所139.9245", "lat": 35.8724, "lon": 139.9245, "source": "osm"},
        "b": {"name": "保健センター前", "lat": 35.8274, "lon": 139.8107, "source": "osm"},
        "c": {"name": "となりの停留所139.9156", "lat": 35.8787, "lon": 139.9156, "source": "osm"},
    }
    pat = [{"odpt:busstopPoleOrder": [{"odpt:busstopPole": k} for k in ("a", "b", "c")]}]
    moved, dropped = odptbus.repair_spikes(stops, pat, geo, "東武バス", log=lambda m: None)
    assert moved == ["保健センター前"] and dropped == []
    assert round(stops["b"]["lat"], 3) == 35.880       # 流山市のほうに替わった
    assert stops["b"]["source"] == "osm"


def test_repair_drops_a_stop_it_cannot_place():
    """**替えが無ければ落とす。** 位置を推測して置かない（この作品の作法）。"""
    geo = odptbus.Geo()
    geo.add("どこかの前", 35.9388, 139.9167, "osm:東武バスセントラル")   # 8km 先の 1 か所だけ
    stops = {
        "a": {"name": "手前", "lat": 35.8673, "lon": 139.9300, "source": "osm"},
        "b": {"name": "どこかの前", "lat": 35.9388, "lon": 139.9167, "source": "osm"},
        "c": {"name": "その先", "lat": 35.8724, "lon": 139.9245, "source": "osm"},
    }
    pat = [{"odpt:busstopPoleOrder": [{"odpt:busstopPole": k} for k in ("a", "b", "c")]}]
    moved, dropped = odptbus.repair_spikes(stops, pat, geo, "東武バス", log=lambda m: None)
    assert moved == [] and dropped == ["どこかの前"]
    assert "b" not in stops


def test_repair_never_drops_the_end_of_a_route():
    """**端は落とさない。** 前後で挟めないので「隣まで遠い」ことしか見えず、
    始発が離れた駅から出る系統と見分けが付かない
    （これで「流山おおたかの森駅西口」が消えた）。"""
    geo = odptbus.Geo()
    geo.add("遠い始発", 35.9500, 139.9200, "osm:東武バスセントラル")
    stops = {
        "a": {"name": "遠い始発", "lat": 35.9500, "lon": 139.9200, "source": "osm"},
        "b": {"name": "次", "lat": 35.8724, "lon": 139.9245, "source": "osm"},
        "c": {"name": "その次", "lat": 35.8730, "lon": 139.9250, "source": "osm"},
    }
    pat = [{"odpt:busstopPoleOrder": [{"odpt:busstopPole": k} for k in ("a", "b", "c")]}]
    moved, dropped = odptbus.repair_spikes(stops, pat, geo, "東武バス", log=lambda m: None)
    assert dropped == []
    assert "a" in stops


def test_the_two_known_bad_stops_are_fixed_in_the_data(nagareyama_con):
    """**取り込んだ結果**でも直っていること（2026-09-30 に作り直した）。"""
    rows = {r["stop_name"]: (r["lat"], r["lon"]) for r in nagareyama_con.execute(
        "SELECT stop_name, lat, lon FROM stop WHERE feed_id='tobubus' "
        "AND (stop_name LIKE '%保健センター%' OR stop_name LIKE '%ショッピングセンター%')")}
    lat, lon = rows["保健センター前（流山市）"]
    assert 35.87 < lat < 35.89 and 139.91 < lon < 139.93      # 草加市ではなく流山市
    lat, lon = rows["ショッピングセンター前（流山市）"]
    assert 35.86 < lat < 35.88 and 139.91 < lon < 139.93      # 野田市ではなく流山市


def test_no_bus_line_jumps_across_the_map(client):
    """**地図の上で線が飛ばない。** ユーザーが気づいたのはここ（2026-09-30）。

    高速バスは 1 区間が 3.5km あるので、それより上を飛びとみなす。
    """
    d = client.get("/api/network?stage=nagareyama").get_json()
    buses = [f for f in d["lines"]["features"] if f["properties"]["kind"] == "bus"]
    assert len(buses) == 30                       # 空を調べて通してしまわないように
    worst = 0
    for f in buses:
        c = f["geometry"]["coordinates"]
        for a, b in zip(c, c[1:]):
            worst = max(worst, odptbus.haversine((a[1], a[0]), (b[1], b[0])))
    assert worst < 4000
