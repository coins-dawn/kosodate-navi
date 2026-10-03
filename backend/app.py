"""Flask アプリ。API と画面の配信。

舞台は ?stage= で切り替える。重いもの（時刻表・歩行グラフ・ほこナビ・構内データ）は
舞台ごとに 1 回だけ読んで使い回す。**まだ使えない舞台**（`enabled: false`）は
一覧に名前だけ出し、画面側で押せないボタンにする。
"""
import json
import math
import time
from datetime import date, datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from backend import (barrier as barrier_mod, gtfs, indoor as indoor_mod,
                     isochrone as isochrone_mod, network as network_mod, plan as plan_mod,
                     profile as profile_mod, spots as spots_mod, stages as stages_mod,
                     transit, walk, weather)

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND = REPO_ROOT / "frontend"

app = Flask(__name__, static_folder=None)
_ctx = {}

SNAPSHOT_PATH = stages_mod.REPO_ROOT / "data" / "snapshots.yaml"
SITE_PATH = stages_mod.REPO_ROOT / "data" / "site.yaml"


class StageContext:
    def __init__(self, stage):
        self.stage = stage
        self.con = gtfs.connect(stage.db_path) if stage.db_path.exists() else None
        self.net = transit.Network(self.con, stage.transfer_db_path) if self.con else None
        self.walk = walk.WalkGraph(stage.walk_db_path)
        self.barrier = barrier_mod.BarrierLayer(stage)
        feed = (stage.indoor or {}).get("feed") if stage.indoor else None
        self.indoor = indoor_mod.IndoorNetwork(self.con, feed) if (self.con and feed) \
            else indoor_mod.IndoorNetwork(None)
        self.spots = spots_mod.SpotIndex(stage)
        self.router = transit.Router(self.net, self.walk, self.indoor) if self.net else None
        # 地図に重ねる「探索が使っている路線と乗り場」。**最初に見に来たときに作る**
        self.overlay = network_mod.TransitOverlay(stage, self.net) if self.net else None
        self.planner = plan_mod.Planner(stage, self.router, self.walk,
                                        self.barrier, self.indoor)
        # 到達圏。**いまの画面は使っていない**（行き先の絞り込みは自宅からの半径にした）。
        # engine はそのまま動くので、戻したくなったら /api/nearby の隣に足せばよい。
        self.iso = isochrone_mod.IsochroneService(self.walk, self.router)


def ctx(stage_id=None):
    stage = stages_mod.load(stage_id)
    if stage.id not in _ctx:
        _ctx[stage.id] = StageContext(stage)
    return _ctx[stage.id]


def parse_profile(args):
    """移動手段（mode）が来ればそちら、来なければ月齢から。"""
    mode = args.get("mode")
    if mode:
        return profile_mod.for_mode(mode)
    months = int(args.get("age_months", 24))
    stroller = args.get("stroller")
    if stroller is not None:
        stroller = str(stroller).lower() in ("1", "true", "yes")
    return profile_mod.for_months(months, stroller)


def parse_home(c, args):
    """自宅は舞台ごとの候補から選ぶ。lat/lon が来ればそちらを優先する。"""
    homes = c.stage.homes
    if args.get("lat") and args.get("lon"):
        return {"id": args.get("home") or "custom", "name": args.get("home_name") or "自宅",
                "lat": float(args["lat"]), "lon": float(args["lon"])}
    wanted = args.get("home")
    for h in homes:
        if h["id"] == wanted:
            return h
    if homes:
        return homes[0]
    return {"id": "center", "name": c.stage.name, **c.stage.center}


def parse_when(args):
    t = args.get("time")
    d = args.get("date")
    on = date.fromisoformat(d) if d else date.today()
    if t:
        hh, _, mm = t.partition(":")
        return int(hh) * 3600 + int(mm or 0) * 60, on
    now = datetime.now()
    return now.hour * 3600 + now.minute * 60, on


# --- 画面 ---

@app.route("/")
def index():
    return send_from_directory(FRONTEND, "index.html")


@app.route("/<path:name>")
def static_files(name):
    return send_from_directory(FRONTEND, name)


# --- API ---

def feed_rows(c, extra_cols=""):
    """取り込んだ時刻表の一覧。**どれだけ入っているか（系統・停留所・便の数）まで出す。**

    名前だけ並べると「バスは入っていないのでは」と読めてしまう。実際、流山市で
    GTFS を出している路線バスは市のぐりーんバス 1 本だけ（東武バスは ODPT の
    チャレンジ限定データ）なので、数字まで見せないと**データの空白**が伝わらない。
    """
    if not c.con:
        return []
    cols = "feed_id, feed_name, organization, kind" + extra_cols
    feeds = [dict(r) for r in c.con.execute(f"SELECT {cols} FROM feed").fetchall()]
    counts = {}
    for table, key in (("route", "routes"), ("stop", "stops"), ("trip", "trips")):
        for fid, n in c.con.execute(f"SELECT feed_id, COUNT(*) FROM {table} GROUP BY feed_id"):
            counts.setdefault(fid, {})[key] = n
    # いちばん多い route_type で鉄道かバスかを決め、数え方の言葉を変える
    kinds = {fid: rt for fid, rt in c.con.execute(
        "SELECT feed_id, route_type FROM route GROUP BY feed_id ORDER BY COUNT(*)")}
    # **静的データの取得日時を画面に出す**（開発者ガイドライン 2.2.1）。
    # 取り込んだ元ファイル（GTFS の zip）の更新日時をそのまま使う
    raw_dir = c.stage.raw_dir / "gtfs"
    for f in feeds:
        f.update(counts.get(f["feed_id"]) or {})
        zip_path = raw_dir / f"{f['feed_id']}.zip"
        if zip_path.exists():
            f["fetched_at"] = time.strftime("%Y-%m-%d",
                                            time.localtime(zip_path.stat().st_mtime))
        rail = kinds.get(f["feed_id"]) in (0, 1, 2)
        f["place_word"] = "駅" if rail else "停留所"
        f["route_word"] = "路線" if rail else "系統"
        # JSON API から組み直したフィードは、**どう補ったか**も出す
        # （東武バスの JSON には停留所の座標が無く、名前で補っている）
        report = raw_dir / f"{f['feed_id']}.report.json"
        if report.exists():
            try:
                f["built"] = json.loads(report.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
    return feeds


@app.route("/api/stages")
def api_stages():
    out = []
    for sid, stage in sorted(stages_mod.load_all(include_disabled=True).items()):
        if not stage.enabled:
            # まだ使えない舞台。**名前だけ返す**（データが無いので中身は作れない）。
            # 画面はこれを押せないボタンにして、後から足す場所を見せておく
            out.append({"stage": sid, "name": stage.name, "enabled": False,
                        "note": stage.note})
            continue
        c = ctx(sid)
        cov = stage.coverage()
        feeds = feed_rows(c)
        cov.update({
            "feeds": feeds,
            "missing_feeds": [{"id": t["id"], "name": t.get("feed_name"),
                               "reason": "チャレンジ用トークンが必要"}
                              for t in stage.transit
                              if not any(f["feed_id"] == t["id"] for f in feeds)],
            "center": stage.center,
            "area_km2": round(stage.area_km2(), 1),
            "bbox": stage.bbox(),
            "boundary_url": f"/api/boundary?stage={sid}",
            "hokonavi_detail": c.barrier.coverage(),
            "indoor_detail": c.indoor.summary(),
            "spots_detail": c.spots.coverage(),
            "homes": stage.homes,
            "modes": profile_mod.load_modes_public(),
            # 静的版（GitHub Pages）は天気をブラウザから直接取りに行くので、
            # 気象庁の区域コードを画面に渡しておく
            "jma_area": stage.jma_area,
        })
        out.append(cov)
    return jsonify(out)


@app.route("/api/snapshots")
def api_snapshots():
    """出発時刻のスナップショット（data/snapshots.yaml）。

    静的版は出発時刻を固定して結果を先に計算する。開発用のサーバでも
    **同じ一覧を返す**ので、画面はどちらでも同じ動きになる。
    """
    import yaml
    return jsonify(yaml.safe_load(SNAPSHOT_PATH.read_text(encoding="utf-8")) or [])


@app.route("/api/site")
def api_site():
    """公開時に画面へ出す情報（問い合わせ先など）。

    **開発者ガイドライン 3.1** は、公開するアプリに問い合わせ先を明記し、
    一般ユーザが交通事業者へ直接問い合わせないようにすることを求めている。
    """
    import yaml
    conf = yaml.safe_load(SITE_PATH.read_text(encoding="utf-8")) or {} \
        if SITE_PATH.exists() else {}
    return jsonify(conf)


@app.route("/api/boundary")
def api_boundary():
    stage = ctx(request.args.get("stage")).stage
    geo = stage.boundary()
    return jsonify(geo or {"type": "FeatureCollection", "features": []})


@app.route("/api/profiles")
def api_profiles():
    return jsonify([
        {"id": b["id"], "label": b["label"], "months": b["months"],
         "tier": b.get("tier"), "stroller": b.get("stroller", False),
         "steps_blocked": b.get("steps") is None}
        for b in profile_mod.load_bands()])


@app.route("/api/home", methods=["POST"])
def api_home():
    """自宅の登録。舞台の外には置けない。"""
    body = request.get_json(force=True, silent=True) or {}
    c = ctx(body.get("stage") or request.args.get("stage"))
    lat, lon = float(body["lat"]), float(body["lon"])
    ok = c.stage.contains(lat, lon)
    return jsonify({"ok": ok, "stage": c.stage.id, "name": c.stage.name,
                    "message": None if ok else f"{c.stage.name}の外には自宅を登録できません"})


@app.route("/api/nearby")
def api_nearby():
    """**自宅から半径 N メートルの中にあるスポット。** この画面の主役。

    **行き先さがしに移動手段は使わない**（2026-09-27・ユーザー指示）。
    以前は到達圏（徒歩・自転車・徒歩＋公共交通で N 分）で絞っていたが、
    手段を替えると出る行き先が変わるのが分かりにくかった。
    半径なら誰が見ても同じ範囲を指し、スライダを動かしたときの変化も素直になる。
    **移動手段の話は、行き先を決めたあとの `/api/routes` に寄せてある。**

    市の外のスポットも混ざる（**円は市境で止まらない**）。
    到達圏の仕組み（`backend/isochrone.py`）は消していないので、戻したくなれば使える。
    """
    started = time.time()
    c = ctx(request.args.get("stage"))
    home = parse_home(c, request.args)
    radius_m = max(1, int(float(request.args.get("radius_m", 1000))))
    _, on = parse_when(request.args)

    # スポットの絞り込みは年齢を見ない（この画面では月齢を聞いていない）
    prof = profile_mod.for_mode("walk")
    radius_km = radius_m / 1000.0
    items, total = c.spots.search(home["lat"], home["lon"], prof, radius_km=radius_km,
                                  limit=4000, on_date=on)
    skipped_parks = c.spots.last_skipped_parks   # 遠くて出さなかった小さい公園
    picks = c.spots.picks(home["lat"], home["lon"], prof, radius_km=radius_km,
                          limit=6, on_date=on)
    outside = [s for s in items if s.get("outside")]
    cities = {}
    for s in outside:
        cities[s.get("city") or "市外"] = cities.get(s.get("city") or "市外", 0) + 1
    return jsonify({
        "home": home, "radius_m": radius_m,
        # 円は画面側で描く（中心と半径があれば引けるので、面を送る必要がない）
        "area_km2": round(math.pi * radius_km ** 2, 2),
        "count": total, "spots": items, "picks": picks,
        "inside": total - len(outside), "outside": len(outside),
        "outside_cities": sorted(cities.items(), key=lambda x: -x[1]),
        "categories": c.spots.categories(items=items),
        "skipped_parks": skipped_parks,
        "elapsed_ms": int((time.time() - started) * 1000),
    })


@app.route("/api/spots")
def api_spots():
    c = ctx(request.args.get("stage"))
    prof = parse_profile(request.args)
    lat = float(request.args.get("lat", c.stage.center["lat"]))
    lon = float(request.args.get("lon", c.stage.center["lon"]))
    radius = float(request.args.get("radius_km", c.stage.home_radius_km))
    _, on = parse_when(request.args)
    items, total = c.spots.search(lat, lon, prof, radius_km=radius, on_date=on)
    return jsonify({"profile": prof.to_json(), "count": total, "shown": len(items),
                    "categories": c.spots.categories(), "spots": items,
                    "picks": c.spots.picks(lat, lon, prof, radius_km=radius, on_date=on),
                    "sources": c.spots.coverage()["sources"]})


@app.route("/api/events")
def api_events():
    """今日以降のイベント。子育てイベントを機械可読で出している自治体はまだ少ない。"""
    c = ctx(request.args.get("stage"))
    _, on = parse_when(request.args)
    items = c.spots.events(on)
    src = [s for s in c.spots.coverage()["sources"] if s["kind"] == "municipal_event"]
    return jsonify({"count": len(items), "events": items, "sources": src,
                    "total_in_data": c.spots.coverage()["events"]})


@app.route("/api/spot/<path:spot_id>")
def api_spot(spot_id):
    c = ctx(request.args.get("stage"))
    spot = c.spots.get(spot_id)
    if not spot:
        return jsonify({"error": "not found"}), 404
    fc = weather.forecast(c.stage.jma_area)
    return jsonify({"spot": spot, "weather": fc, "advice": weather.advice(spot, fc)})


@app.route("/api/routes", methods=["POST"])
def api_routes():
    body = request.get_json(force=True, silent=True) or {}
    args = {**request.args, **body}
    c = ctx(args.get("stage"))
    if not c.router:
        return jsonify({"error": "この舞台の時刻表がまだ取り込まれていません"}), 503
    prof = parse_profile(args)
    when, on = parse_when(args)
    origin = (float(args["from_lat"]), float(args["from_lon"]), args.get("from_name", "自宅"))
    dest = (float(args["to_lat"]), float(args["to_lon"]), args.get("to_name", "目的地"))
    # **自転車と車はさがさない**（2026-09-30・ユーザー指示）。公共交通ではないうえ、
    # 推奨されているオープンデータを 1 つも使っていない（自前グラフと OSM だけ）。
    # `modes` に "bike" / "car" を渡せば今までどおり返る（engine は消していない）
    modes = args.get("modes") or ["total", "train", "bus", "walk"]
    if isinstance(modes, str):
        modes = modes.split(",")

    started = time.time()
    out = {"profile": prof.to_json(), "date": on.isoformat(), "when": when}
    if {"total", "train", "bus"} & set(modes):
        results = c.planner.transit_plans(origin, dest, when, prof, on_date=on)
        out["transit"] = results
        out["total"] = results[0] if results else None
        out["train"] = c.planner.pick_kind(results, "train")
        out["bus"] = c.planner.pick_kind(results, "bus")
    for mode in ("walk", "bike", "car"):
        if mode in modes:
            out[mode] = c.planner.walk_plan(origin, dest, when, prof, mode)
    picked = c.planner.pick_total(out)
    if picked is not None or "total" in out:
        out["total"] = picked
    out["elapsed"] = round(time.time() - started, 2)
    out["hokonavi"] = c.barrier.coverage()["available"]
    return jsonify(out)


@app.route("/api/station/<station_id>/entrances")
def api_entrances(station_id):
    c = ctx(request.args.get("stage"))
    if not c.indoor.available:
        return jsonify({"available": False, "entrances": []})
    prof = parse_profile(request.args)
    return jsonify({"available": True,
                    "station": c.indoor.stations.get(station_id, {}).get("stop_name"),
                    "entrances": c.indoor.station_entrances(station_id, prof),
                    "step_free": [e["id"] for e in c.indoor.step_free_entrances(station_id)]})


@app.route("/api/stations")
def api_stations():
    c = ctx(request.args.get("stage"))
    if not c.indoor.available:
        return jsonify({"available": False, "stations": []})
    out = []
    for sid, s in c.indoor.stations.items():
        free = c.indoor.step_free_entrances(sid)
        out.append({"id": sid, "name": s["stop_name"], "lat": s["lat"], "lon": s["lon"],
                    "entrances": len(c.indoor.entrances.get(sid, [])),
                    "step_free": len(free),
                    "step_free_names": [e["name"] for e in free]})
    out.sort(key=lambda x: x["name"])
    return jsonify({"available": True, "stations": out})


@app.route("/api/network")
def api_network():
    """**経路探索が使っている路線と乗り場。** 地図に重ねて、チェックで消せるようにする。

    出すのは探索が実際に使っているものだけで、飾りの路線図ではない
    （点は `transit.Network.places` ＝探索の単位そのもの）。
    """
    c = ctx(request.args.get("stage"))
    if not c.overlay:
        return jsonify({"available": False, "lines": {"type": "FeatureCollection",
                                                      "features": []},
                        "stops": {"type": "FeatureCollection", "features": []},
                        "feeds": [], "counts": {}})
    return jsonify({"available": True, **c.overlay.geojson()})


@app.route("/api/coverage")
def api_coverage():
    """その舞台で何のデータが使えて、何が使えないか。作品の主張をそのまま返す。"""
    c = ctx(request.args.get("stage"))
    feeds = feed_rows(c, extra_cols=", license")
    missing = [t for t in c.stage.transit
               if not any(f["feed_id"] == t["id"] for f in feeds)]
    return jsonify({
        "stage": c.stage.id, "name": c.stage.name, "area_km2": round(c.stage.area_km2(), 1),
        "feeds": feeds,
        "missing_feeds": [{"id": t["id"], "name": t.get("feed_name"),
                           "reason": "チャレンジ用トークンが必要"} for t in missing],
        "walk_graph": c.walk.available,
        "hokonavi": c.barrier.coverage(),
        "indoor": c.indoor.summary(),
        "spots": c.spots.coverage(),
    })


@app.route("/api/weather")
def api_weather():
    c = ctx(request.args.get("stage"))
    area = c.stage.jma_area
    return jsonify({"forecast": weather.forecast(area), "warnings": weather.warnings(area)})


if __name__ == "__main__":
    # Vagrant のポートフォワードで使うので 0.0.0.0 で待ち受ける
    app.run(host="0.0.0.0", port=8002, debug=True)
