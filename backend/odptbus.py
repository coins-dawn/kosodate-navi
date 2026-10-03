"""**ODPT の「バスの JSON API」を GTFS に組み直す。**

ODPT のバスデータには 2 つの届き方がある。

- **GTFS のファイル**（流山ぐりーんバス・都営バスなど）→ そのまま `gtfs.py` が読める
- **JSON API**（`odpt:BusstopPole` / `odpt:BusroutePattern` / `odpt:BusTimetable`）
  → 東武バスはこちら。**チャレンジ2026 限定データ**で、GTFS は配信されていない

このモジュールは後者を**GTFS の zip に書き出す**。こうすると取り込み・探索・
静的サイトの作り方が GTFS のフィードと 1 ミリも変わらない
（`build_db.py` から先は東武バスだと意識しなくてよくなる）。

**いちばんの壁は座標。** `odpt:BusstopPole` は**停留所の座標を持っていない**
（東武バス 4,728 件すべてで `geo:lat` が無い）。位置が無いと地図にも出せず、
歩いて行ける停留所も分からない。そこで**名前で座標を補う**:

1. **すでに取り込んだ GTFS の `stops.txt`**（流山ぐりーんバスなど。事業者が出した公式の座標）
2. **OpenStreetMap のバス停**（`scripts/build_busstop_geo.py` が作った `data/busstop/<stage>.json`）

どちらでも当たらない停留所は**出さない**（位置を推測して置かない）。
落とした数は `data/raw/<stage>/gtfs/<id>.report.json` に残し、画面の
「この舞台で使えるデータ」に出す。

**名前で当てると、たまに別の市に飛ぶ。** 「保健センター前」「ショッピングセンター前」の
ようなありふれた名前は同じ名前が別の市にもあり、事業者名とかたまりの大きさだけでは
選び違える（実測: 西初石系統の「保健センター前」が**草加市の同名停留所**に当たって
11km 飛んでいた）。**系統の並びで見ると、そこだけ寄り道が異常に大きい**ので、
`repair_spikes` が前後の停留所から見て飛んでいる点を見つけ、
**同じ名前のほかのかたまりに替えるか、替えられなければ落とす**。
"""
import csv
import io
import json
import math
import re
import time
import unicodedata
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, timedelta

API = "https://api-challenge.odpt.org/api/v4/"
UA = {"User-Agent": "kosodate-navi (odc2026)"}
SLEEP = 1.1                  # レート制限は 60 リクエスト/分
# **飛んだ座標の見分け方。** 前 → その停留所 → 次 と回る道のりが、前 → 次 をまっすぐ
# 行くのに比べて大きすぎたら、そこだけ別の場所に当たっている。
# 高速バスのように 1 区間が長い系統もあるので、**比**で見て、**下限**も置く
# （まっすぐ行っても長い区間は、寄り道の比が小さくなるので引っかからない）
SPIKE_MIN_M = 1500           # これ以下の寄り道は見逃す（普通の遠回り）
SPIKE_RATIO = 3.0            # まっすぐ行く距離の何倍から飛びとみなすか
END_MIN_M = 3000             # 端の停留所は前後で挟めないので、隣までの距離で見る
END_RATIO = 6.0              # その系統の区間の中央値の何倍から飛びとみなすか
HOLIDAY_CSV = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv"

# odpt のカレンダー → GTFS の曜日
CALENDARS = {
    "odpt.Calendar:Weekday": (1, 1, 1, 1, 1, 0, 0),
    "odpt.Calendar:Saturday": (0, 0, 0, 0, 0, 1, 0),
    "odpt.Calendar:Holiday": (0, 0, 0, 0, 0, 0, 1),
    "odpt.Calendar:SaturdayHoliday": (0, 0, 0, 0, 0, 1, 1),
    "odpt.Calendar:Sunday": (0, 0, 0, 0, 0, 0, 1),
    "odpt.Calendar:Everyday": (1, 1, 1, 1, 1, 1, 1),
}
WEEKDAY_LIKE = {"odpt.Calendar:Weekday"}          # 祝日は運休にする側
HOLIDAY_LIKE = {"odpt.Calendar:Holiday", "odpt.Calendar:SaturdayHoliday",
                "odpt.Calendar:Sunday"}           # 祝日に動く側


def norm_stop_name(name):
    """停留所名を照合しやすい形にする。**この規則は座標表と共有する。**

    全角の英数を半角に直し、「（流山市）」のような**補足の括弧を落とし**、
    空白を詰め、ゆれのある字（ヶ／ケ、･／・）をそろえる。
    """
    s = unicodedata.normalize("NFKC", name or "")
    s = re.sub(r"[（(][^）)]*[)）]", "", s)
    s = re.sub(r"\s+", "", s)
    return s.replace("･", "・").replace("ヶ", "ケ").replace("ヵ", "ケ")


def get_json(endpoint, params, token):
    q = dict(params)
    q["acl:consumerKey"] = token
    url = API + endpoint + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as res:
        return json.loads(res.read().decode("utf-8"))


def hhmm_to_sec(text):
    if not text:
        return None
    h, _, m = text.partition(":")
    return int(h) * 3600 + int(m) * 60


def fmt_sec(sec):
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


class Geo:
    """停留所名 → 座標。**公式の GTFS を先に、OSM をあとに**引く。"""

    def __init__(self):
        self.table = defaultdict(list)

    def add(self, name, lat, lon, source):
        self.table[norm_stop_name(name)].append((lat, lon, source))

    @classmethod
    def build(cls, stage, feed_id):
        geo = cls()
        # 1) すでに取り込んだ GTFS の停留所（事業者が出した公式の座標）
        gtfs_dir = stage.raw_dir / "gtfs"
        for stops in sorted(gtfs_dir.glob("*/stops.txt")):
            if stops.parent.name == feed_id:
                continue
            with stops.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("stop_lat") and row.get("stop_lon"):
                        geo.add(row.get("stop_name"), float(row["stop_lat"]),
                                float(row["stop_lon"]), "gtfs")
        # 2) OSM のバス停（名前で引ける表を先に作ってある）
        from backend import stages as stages_mod
        path = stages_mod.REPO_ROOT / "data" / "busstop" / f"{stage.id}.json"
        if path.exists():
            for key, rows in json.loads(path.read_text(encoding="utf-8"))["stops"].items():
                for lat, lon, operator in rows:
                    geo.table[key].append((lat, lon, "osm:" + (operator or "")))
        return geo

    def candidates(self, name, operator_hint=""):
        """名前に当たる**かたまり**を、良さそうな順に返す（`(lat, lon, source)` の並び）。

        「向原」のように 17km 離れた同名の停留所がある。**150m 以内をひとかたまり**に
        まとめ、その事業者の名前が入っているものを優先し、次に点の多いかたまりを採る。

        **1 位が正しいとは限らない**（事業者名は別の市の同名停留所にも付いている）ので、
        2 位以下も返す。系統の並びで見て飛んでいたら `repair_spikes` が選び直す。
        """
        rows = self.table.get(norm_stop_name(name)) or []
        if not rows:
            return []
        clusters = []
        for lat, lon, source in rows:
            for c in clusters:
                if abs(c["lat"] - lat) < 0.0014 and abs(c["lon"] - lon) < 0.0017:
                    c["rows"].append((lat, lon, source))
                    break
            else:
                clusters.append({"lat": lat, "lon": lon, "rows": [(lat, lon, source)]})

        def score(c):
            hit = any(operator_hint and operator_hint in s for _, _, s in c["rows"])
            official = any(s == "gtfs" for _, _, s in c["rows"])
            return (hit, official, len(c["rows"]))

        out = []
        for c in sorted(clusters, key=score, reverse=True):
            lat = sum(r[0] for r in c["rows"]) / len(c["rows"])
            lon = sum(r[1] for r in c["rows"]) / len(c["rows"])
            source = "gtfs" if any(r[2] == "gtfs" for r in c["rows"]) else "osm"
            out.append((round(lat, 6), round(lon, 6), source))
        return out

    def find(self, name, operator_hint=""):
        """名前から 1 点だけ選ぶ（いちばん良さそうなかたまり）。"""
        got = self.candidates(name, operator_hint)
        return got[0] if got else None


def haversine(a, b):
    """`(lat, lon)` どうしの距離（m）。"""
    (y1, x1), (y2, x2) = a, b
    p1, p2 = math.radians(y1), math.radians(y2)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(x2 - x1) / 2) ** 2)
    return 2 * 6371000.0 * math.asin(math.sqrt(h))


def is_spike(prev, cur, nxt):
    """前 → ここ → 次 の寄り道が、まっすぐ行くのに比べて大きすぎるか。"""
    detour = haversine(prev, cur) + haversine(cur, nxt)
    return detour > max(SPIKE_MIN_M, SPIKE_RATIO * haversine(prev, nxt))


def repair_spikes(stops, patterns, geo, operator_hint, log=print):
    """**名前で当てた座標が飛んでいる停留所を直す。**

    名前だけで当てると、ありふれた名前は別の市の同名停留所に当たることがある。
    1 件ずつ見ても気づけないが、**系統の並びで見ると、そこだけ寄り道が異常**になる。

    1. 前後から見て飛んでいたら、**同じ名前のほかのかたまり**で飛ばないものに替える
    2. 替えられるものが無ければ、**その停留所は落とす**（位置を推測して置かない）

    **端の停留所は落とさない。** 前後で挟めないので「隣まで遠い」ことしか見えず、
    始発が離れた駅から出る系統と見分けが付かない。実際、これで落とすと
    **「流山おおたかの森駅西口」が消えた**（おおたかの森を起点に遠くへ向かう系統がある）。
    端は**替えが見つかったときだけ替え**、見つからなければそのままにする。
    落とすのは系統の途中で飛んでいるものだけ。

    直した件数と落とした件数を返す（レポートに残して画面に出す）。
    """
    def pt(pid):
        return (stops[pid]["lat"], stops[pid]["lon"])

    def repick(pid, ok):
        """飛んでいない置き場所に替える。替えられなければ False。"""
        best = None
        for lat, lon, source in geo.candidates(stops[pid]["name"], operator_hint):
            if not ok((lat, lon)):
                continue
            if best is None or ok((lat, lon)) > best[0]:
                best = (ok((lat, lon)), lat, lon, source)
        if best is None:
            return False
        stops[pid].update(lat=best[1], lon=best[2], source=best[3], repaired=True)
        return True

    moved, dropped = [], []
    for pat in patterns:
        order = [o["odpt:busstopPole"] for o in pat.get("odpt:busstopPoleOrder") or []]
        # **落とした停留所は前後の判定にも使わない**ので、その都度 seq から抜く
        seq = [p for p in order if p in stops]
        i = 0
        while i < len(seq):
            if len(seq) < 3:
                break
            hops = sorted(haversine(pt(a), pt(b)) for a, b in zip(seq, seq[1:]))
            limit = max(END_MIN_M, END_RATIO * hops[len(hops) // 2])
            pid = seq[i]
            edge = i == 0 or i == len(seq) - 1
            if edge:
                # 端は前後で挟めないので、**隣までの距離**で見る。近い置き場所ほど良い
                other = pt(seq[1] if i == 0 else seq[-2])
                if haversine(pt(pid), other) <= limit:
                    i += 1
                    continue
                ok = (lambda c, o=other, lim=limit:
                      (lim - haversine(o, c)) if haversine(o, c) <= lim else 0)
            else:
                a, b = pt(seq[i - 1]), pt(seq[i + 1])
                if not is_spike(a, pt(pid), b):
                    i += 1
                    continue
                ok = (lambda c, a=a, b=b: 0 if is_spike(a, c, b)
                      else 1.0 / (1.0 + haversine(a, c) + haversine(c, b)))
            name = stops[pid]["name"]
            if repick(pid, ok):
                moved.append(name)
                i += 1
            elif edge:
                i += 1                  # **端は落とさない**（遠い始発と見分けが付かない）
            else:
                dropped.append(name)
                stops.pop(pid)
                seq.pop(i)              # 抜けたぶん、次は同じ位置を見直す
    if moved or dropped:
        log(f"   座標が飛んでいた停留所: 置き直し {len(moved)} / 落とした {len(dropped)}"
            + (f"（{'・'.join(sorted(set(dropped)))}）" if dropped else ""))
    return sorted(set(moved)), sorted(set(dropped))


def japanese_holidays(start, end):
    """内閣府の「国民の祝日」CSV（鍵不要）。取れなければ空で続ける。"""
    try:
        req = urllib.request.Request(HOLIDAY_CSV, headers=UA)
        with urllib.request.urlopen(req, timeout=60) as res:
            text = res.read().decode("shift_jis", errors="replace")
    except Exception:                              # noqa: BLE001
        return []
    out = []
    for row in csv.reader(io.StringIO(text)):
        if not row or "/" not in row[0]:
            continue
        try:
            y, m, d = (int(v) for v in row[0].split("/"))
        except ValueError:
            continue
        day = date(y, m, d)
        if start <= day <= end:
            out.append(day)
    return out


def select_patterns(stage, patterns, poles, geo, operator_hint):
    """**舞台の中を通る系統**だけ選ぶ（全国 863 系統から絞る）。"""
    chosen = []
    for pat in patterns:
        for order in pat.get("odpt:busstopPoleOrder") or []:
            pole = poles.get(order["odpt:busstopPole"])
            name = (pole or {}).get("dc:title") or order.get("odpt:note") or ""
            found = geo.find(name, operator_hint)
            if found and stage.contains(found[0], found[1]):
                chosen.append(pat)
                break
    return chosen


def build_zip(stage, feed, token, out_zip, log=print):
    """ODPT のバス JSON API を GTFS の zip にして書き出す。"""
    operator = feed["operator"]                    # 例: odpt.Operator:TobuBus
    name = feed.get("organization") or operator.split(":")[-1]
    hint = feed.get("osm_operator") or ""
    feed_id = feed["id"]

    log(f"  {name}: 停留所・系統を取りに行きます")
    poles = {p["owl:sameAs"]: p
             for p in get_json("odpt:BusstopPole", {"odpt:operator": operator}, token)}
    patterns = get_json("odpt:BusroutePattern", {"odpt:operator": operator}, token)
    log(f"   停留所 {len(poles):,} / 系統 {len(patterns):,}（全国ぶん）")

    geo = Geo.build(stage, feed_id)
    chosen = select_patterns(stage, patterns, poles, geo, hint)
    log(f"   {stage.name} を通る系統 {len(chosen)}")

    # 便（時刻表）は系統ごとに取る。まとめて取ると 1,000 件で頭打ちになる
    timetables = []
    for i, pat in enumerate(chosen, 1):
        timetables += get_json("odpt:BusTimetable",
                               {"odpt:busroutePattern": pat["owl:sameAs"]}, token)
        if i % 10 == 0 or i == len(chosen):
            log(f"   時刻表 {i}/{len(chosen)} 系統（便 {len(timetables):,}）")
        time.sleep(SLEEP)                          # レート制限 60/分

    # --- 停留所（座標の付いたものだけ） ---
    stops, missing = {}, {}
    for pid in {o["odpt:busstopPole"] for p in chosen
                for o in p.get("odpt:busstopPoleOrder") or []}:
        pole = poles.get(pid)
        title = (pole or {}).get("dc:title")
        if not title:
            continue
        found = geo.find(title, hint)
        if found:
            stops[pid] = {"name": title, "lat": found[0], "lon": found[1],
                          "source": found[2]}
        else:
            missing[pid] = title

    # **名前で当てた座標が別の市に飛んでいないか、系統の並びで見て直す。**
    # 直せないものは落とす（そのぶん `missing` と同じ扱いで、便からも外れる）
    repaired, flown = repair_spikes(stops, chosen, geo, hint, log)

    # --- 便と時刻 ---
    pattern_of = {p["owl:sameAs"]: p for p in chosen}
    trips, stop_times = [], []
    used_calendars, skipped_trips = set(), 0
    for tt in timetables:
        pat = pattern_of.get(tt.get("odpt:busroutePattern"))
        if pat is None:
            continue
        cal = tt.get("odpt:calendar")
        if cal not in CALENDARS:
            skipped_trips += 1
            continue
        rows, prev = [], None
        for obj in tt.get("odpt:busTimetableObject") or []:
            pid = obj.get("odpt:busstopPole")
            if pid not in stops:
                continue                            # 座標が分からない停留所は出さない
            dep = hhmm_to_sec(obj.get("odpt:departureTime")
                              or obj.get("odpt:arrivalTime"))
            arr = hhmm_to_sec(obj.get("odpt:arrivalTime")
                              or obj.get("odpt:departureTime"))
            if dep is None or arr is None:
                continue
            # 日をまたぐ便は時刻が戻るので 24 時間足す（GTFS は 25:10 と書ける）
            while prev is not None and arr < prev:
                arr += 24 * 3600
                dep += 24 * 3600
            if dep < arr:
                dep = arr
            prev = dep
            rows.append((len(rows) + 1, pid, arr, dep,
                         0 if obj.get("odpt:canGetOn", True) else 1,
                         0 if obj.get("odpt:canGetOff", True) else 1))
        if len(rows) < 2:
            skipped_trips += 1
            continue                                # 1 つしか残らない便は経路にならない
        trip_id = tt["owl:sameAs"]
        head = (tt.get("odpt:note") or "").split(":")
        trips.append({
            "route_id": pat.get("odpt:busroute") or pat["owl:sameAs"],
            "service_id": cal.split(":")[-1],
            "trip_id": trip_id,
            "trip_headsign": head[1] if len(head) > 1 else (pat.get("dc:title") or ""),
            "direction_id": 1 if str(pat.get("odpt:direction")) == "2" else 0,
        })
        used_calendars.add(cal)
        for seq, pid, arr, dep, on, off in rows:
            stop_times.append({"trip_id": trip_id, "arrival_time": fmt_sec(arr),
                               "departure_time": fmt_sec(dep), "stop_id": pid,
                               "stop_sequence": seq, "pickup_type": on,
                               "drop_off_type": off})

    # --- 路線（系統） ---
    routes = {}
    for pat in chosen:
        rid = pat.get("odpt:busroute") or pat["owl:sameAs"]
        title = pat.get("dc:title") or ""
        routes.setdefault(rid, {"route_id": rid, "agency_id": feed_id,
                                "route_short_name": title,
                                "route_long_name": f"{name} {title}".strip(),
                                "route_type": 3})

    # --- 暦 ---
    today = date.today()
    start, end = today - timedelta(days=7), today + timedelta(days=180)
    calendar_rows = []
    for cal in sorted(used_calendars):
        mon, tue, wed, thu, fri, sat, sun = CALENDARS[cal]
        calendar_rows.append({
            "service_id": cal.split(":")[-1], "monday": mon, "tuesday": tue,
            "wednesday": wed, "thursday": thu, "friday": fri, "saturday": sat,
            "sunday": sun, "start_date": start.strftime("%Y%m%d"),
            "end_date": end.strftime("%Y%m%d")})
    exceptions = []
    for day in japanese_holidays(start, end):
        stamp = day.strftime("%Y%m%d")
        for cal in used_calendars:
            sid = cal.split(":")[-1]
            if cal in WEEKDAY_LIKE and day.weekday() < 5:
                exceptions.append({"service_id": sid, "date": stamp, "exception_type": 2})
            elif cal in HOLIDAY_LIKE and day.weekday() < 5:
                exceptions.append({"service_id": sid, "date": stamp, "exception_type": 1})

    report = {
        "operator": operator, "patterns": len(chosen), "trips": len(trips),
        "stops": len(stops), "stops_without_location": len(missing),
        "stops_from_gtfs": sum(1 for s in stops.values() if s["source"] == "gtfs"),
        "stops_from_osm": sum(1 for s in stops.values() if s["source"] != "gtfs"),
        "missing_names": sorted(set(missing.values())),
        # 名前で当てた座標が別の市に飛んでいたもの（系統の並びで気づいて直した／落とした）
        "stops_repaired": repaired, "stops_flown_away": flown,
        "skipped_trips": skipped_trips,
        "holidays": len({e["date"] for e in exceptions}),
        "built_at": today.isoformat(),
    }

    def table(rows, fields):
        buf = io.StringIO(newline="")
        w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue()

    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("agency.txt", table(
            [{"agency_id": feed_id, "agency_name": name,
              "agency_url": feed.get("url") or "https://www.odpt.org/",
              "agency_timezone": "Asia/Tokyo", "agency_lang": "ja"}],
            ["agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang"]))
        z.writestr("stops.txt", table(
            [{"stop_id": pid, "stop_name": s["name"], "stop_lat": s["lat"],
              "stop_lon": s["lon"], "location_type": 0} for pid, s in sorted(stops.items())],
            ["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type"]))
        z.writestr("routes.txt", table(
            sorted(routes.values(), key=lambda r: r["route_id"]),
            ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"]))
        z.writestr("trips.txt", table(
            sorted(trips, key=lambda t: t["trip_id"]),
            ["route_id", "service_id", "trip_id", "trip_headsign", "direction_id"]))
        z.writestr("stop_times.txt", table(
            stop_times, ["trip_id", "arrival_time", "departure_time", "stop_id",
                         "stop_sequence", "pickup_type", "drop_off_type"]))
        z.writestr("calendar.txt", table(
            calendar_rows, ["service_id", "monday", "tuesday", "wednesday", "thursday",
                            "friday", "saturday", "sunday", "start_date", "end_date"]))
        if exceptions:
            z.writestr("calendar_dates.txt", table(
                exceptions, ["service_id", "date", "exception_type"]))
        z.writestr("feed_info.txt", table(
            [{"feed_publisher_name": name,
              "feed_publisher_url": "https://www.odpt.org/",
              "feed_lang": "ja", "feed_start_date": start.strftime("%Y%m%d"),
              "feed_end_date": end.strftime("%Y%m%d"),
              "feed_version": today.strftime("%Y%m%d")}],
            ["feed_publisher_name", "feed_publisher_url", "feed_lang",
             "feed_start_date", "feed_end_date", "feed_version"]))

    report_path = out_zip.with_suffix(".report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"   停留所 {len(stops)}（公式 {report['stops_from_gtfs']} / OSM "
        f"{report['stops_from_osm']}）・便 {len(trips):,}・位置が分からず外した停留所 "
        f"{len(missing)} → {out_zip.name}")
    return report
