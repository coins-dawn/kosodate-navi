"""S10 API。舞台の切り替えと、駅の出入口・スポット・経路が JSON で返ること。"""
import json


def test_stages(client):
    d = client.get("/api/stages").get_json()
    ids = {s["stage"] for s in d}
    assert ids == {"shinjuku", "nagareyama", "minato"}
    nagareyama = [s for s in d if s["stage"] == "nagareyama"][0]
    assert nagareyama["enabled"] is True
    assert nagareyama["hokonavi_detail"]["available"] is False


def test_disabled_stage_is_listed_with_name_only(client):
    """**押せないボタンの材料だけ返す。** 中身（区域・時刻表・スポット）は持たない。"""
    d = client.get("/api/stages").get_json()
    stub = [s for s in d if s["stage"] == "shinjuku"][0]
    assert stub["enabled"] is False
    assert stub["name"] == "新宿区"
    assert stub["note"]
    assert "feeds" not in stub and "homes" not in stub


def test_home_outside_is_rejected(client):
    r = client.post("/api/home?stage=nagareyama",
                    json={"lat": 35.6938, "lon": 139.7036})
    d = r.get_json()
    assert d["ok"] is False and "外には" in d["message"]


def test_home_inside_is_accepted(client):
    d = client.post("/api/home?stage=nagareyama",
                    json={"lat": 35.8700, "lon": 139.9220}).get_json()
    assert d["ok"] is True


def test_spots_change_with_age(client):
    base = "/api/spots?stage=nagareyama&lat=35.8562&lon=139.9029&age_months="
    baby = client.get(base + "6").get_json()
    school = client.get(base + "96").get_json()
    assert baby["count"] > school["count"]
    assert baby["profile"]["steps_blocked"] is True
    assert school["profile"]["steps_blocked"] is False


def test_entrances_are_absent_without_pathways(client):
    # いまの舞台に GTFS-Pathways は無い。**無いことを言う**のが仕事
    d = client.get("/api/stations?stage=nagareyama").get_json()
    assert d["available"] is False


def test_coverage_says_what_is_missing(client):
    """**無いものを「無い」と言う**のが「使えるデータ」の欄の仕事。

    東武鉄道は 2026-09-25 にチャレンジ用トークンが届いて取り込めたので、
    いまは `feeds` の側にいる（設定にあって取り込めていないフィードだけが
    `missing_feeds` に出る）。
    """
    d = client.get("/api/coverage?stage=nagareyama").get_json()
    assert d["hokonavi"]["available"] is False
    assert d["indoor"]["available"] is False
    ids = {f["feed_id"] for f in d["feeds"]}
    assert {"mir_train", "greenbus", "tobu"} <= ids
    assert all(f["id"] not in ids for f in d["missing_feeds"])
    assert all(f.get("reason") for f in d["missing_feeds"])


def test_routes(client):
    body = {"stage": "nagareyama", "from_lat": 35.8562, "from_lon": 139.9029,
            "to_lat": 35.8710, "to_lon": 139.9260, "to_name": "流山おおたかの森",
            "age_months": 96, "date": "2026-09-24", "time": "09:00",
            "modes": ["total", "walk"]}
    d = client.post("/api/routes", data=json.dumps(body),
                    content_type="application/json").get_json()
    assert d["total"] is not None
    assert d["total"]["legs"]
    assert d["profile"]["label"] == "6〜12歳"


def test_frontend_is_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200


def test_feeds_carry_their_size(client):
    """時刻表は**名前だけでなく規模まで**返す。

    「流山市にバスは入っていないのでは」と読まれたのが理由（2026-09-23）。
    流山ぐりーんバスは 6 系統・181 停留所あり、**入っている**ことが数字で分かる。
    """
    stages = {s["stage"]: s for s in client.get("/api/stages").get_json()}
    feeds = {f["feed_name"]: f for f in stages["nagareyama"]["feeds"]}
    bus = feeds["流山ぐりーんバス"]
    assert bus["routes"] > 0 and bus["stops"] > 100 and bus["trips"] > 0
    assert (bus["route_word"], bus["place_word"]) == ("系統", "停留所")
    train = feeds["つくばエクスプレス"]
    assert (train["route_word"], train["place_word"]) == ("路線", "駅")
    # 「このサービスについて」も同じ数字を使う
    cov = client.get("/api/coverage?stage=nagareyama").get_json()
    assert {f["feed_name"]: f["stops"] for f in cov["feeds"]}["流山ぐりーんバス"] == bus["stops"]
