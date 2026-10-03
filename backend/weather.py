"""天気と暑さ。気象庁の予報 JSON と環境省の熱中症警戒アラートを使う（どちらも鍵不要）。

屋外のスポット（公園など）に注意を出すのと、経路で屋根のある道を優先するのに使う。
取れなかったときは**黙って何も出さない**（画面は壊さない）。
"""
import json
import os
import time
import urllib.request

JMA_FORECAST = "https://www.jma.go.jp/bosai/forecast/data/forecast/{area}.json"
JMA_WARNING = "https://www.jma.go.jp/bosai/warning/data/warning/{area}.json"
HEAT_ALERT = "https://www.wbgt.env.go.jp/alert/dl/{date}/alert_{date}.csv"
UA = {"User-Agent": "kosodate-navi (odc2026)"}
CACHE_SEC = 900

_cache = {}


def _get(url, parse="json"):
    now = time.time()
    hit = _cache.get(url)
    if hit and now - hit[0] < CACHE_SEC:
        return hit[1]
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as res:
            raw = res.read()
        value = json.loads(raw.decode("utf-8")) if parse == "json" else raw.decode("utf-8")
    except Exception:                                # noqa: BLE001
        value = None
    _cache[url] = (now, value)
    return value


def forecast(area_code):
    """今日の天気・最高気温・降水確率。取れなければ None。

    **静的版を作るときは取りに行かない**（`KOSODATE_NO_WEATHER=1`）。
    先に計算した結果に天気を焼き込むと、数日後に「今日の天気」が嘘になるため。
    静的版ではブラウザから気象庁の JSON を直接取る（CORS が開いている）。
    """
    if os.environ.get("KOSODATE_NO_WEATHER"):
        return None
    data = _get(JMA_FORECAST.format(area=area_code))
    if not data:
        return None
    out = {}
    try:
        series = data[0]["timeSeries"]
        out["weather"] = series[0]["areas"][0]["weathers"][0]
        pops = series[1]["areas"][0].get("pops") if len(series) > 1 else None
        out["pops"] = pops
        for s in data[0]["timeSeries"]:
            areas = s["areas"][0]
            if "temps" in areas:
                temps = [int(t) for t in areas["temps"] if t not in ("", None)]
                if temps:
                    out["temp_max"] = max(temps)
        out["published"] = data[0].get("reportDatetime")
    except (KeyError, IndexError, ValueError):
        return None
    return out


def warnings(area_code):
    data = _get(JMA_WARNING.format(area=area_code))
    if not data:
        return []
    out = []
    for area in data.get("areaTypes", []):
        for a in area.get("areas", []):
            for w in a.get("warnings", []):
                if w.get("status") in ("発表", "継続"):
                    out.append({"area": a.get("name") or a.get("code"), "code": w.get("code")})
    return out


WEATHER_NG = ("雨", "雪", "雷")


def advice(spot, fc):
    """スポットの吹き出しに出す注意。肯定形と注意を分けて返す。"""
    notes = []
    if not fc:
        return notes
    outdoor = not spot.get("indoor", True)
    weather = fc.get("weather") or ""
    temp = fc.get("temp_max")
    pops = fc.get("pops") or []
    max_pop = max((int(p) for p in pops if str(p).isdigit()), default=None)
    if outdoor:
        if any(k in weather for k in WEATHER_NG):
            notes.append({"level": "warn", "text": f"今日の天気は「{weather}」です。屋根のある行き先も見てみてください"})
        elif max_pop is not None and max_pop >= 50:
            notes.append({"level": "warn", "text": f"降水確率が{max_pop}%あります。雨具の用意を"})
        if temp is not None and temp >= 35:
            notes.append({"level": "warn",
                          "text": f"最高気温{temp}℃の猛暑日です。ベビーカーは地面に近く暑くなります"})
        elif temp is not None and temp >= 31:
            notes.append({"level": "info", "text": f"最高気温{temp}℃です。日陰と水分を用意してください"})
    else:
        if any(k in weather for k in WEATHER_NG):
            notes.append({"level": "info", "text": "屋内なので、雨の日でも過ごせます"})
    return notes
