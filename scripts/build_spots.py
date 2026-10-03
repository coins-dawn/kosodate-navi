#!/usr/bin/env python3.9
"""自治体のオープンデータから、子育てスポットを 1 つの形にそろえる。

    python3.9 scripts/build_spots.py [stage_id]

出力: data/spots/generated/<stage>.json
座標の無いデータは国土地理院の住所検索（鍵不要）でジオコーディングし、結果を使い回す。
列の意味と年齢の割り当ては、この下の SPOT_AGES と base_spot() を参照。
"""
import csv
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import spots as spots_mod, stages as stages_mod  # noqa: E402

GSI = "https://msearch.gsi.go.jp/address-search/AddressSearch?q="
OUT_DIR = stages_mod.REPO_ROOT / "data" / "spots" / "generated"
CACHE = stages_mod.REPO_ROOT / "data" / "spots" / "geocode_cache.json"

# 推奨データセット「公共施設一覧」の POI コード → 施設の種類
POI_TYPES = {
    "1402a": "保育所",
    "1403a": "児童館・児童センター",
    "1404a": "学童クラブ",
    "1406a": "子育て支援センター",
    "1501a": "幼稚園",
}

# 名前から施設の種類を拾う（POI コードや種別欄が無いとき）
NAME_TYPES = [
    ("子育て支援センター", "子育て支援センター"),
    ("子育てひろば", "子育てひろば"),
    ("つどいの広場", "子育てひろば"),
    ("親と子のひろば", "子育てひろば"),
    ("子ども家庭支援センター", "子ども家庭支援センター"),
    # 港区の呼び名。「子ども中高生プラザ」「児童高齢者交流プラザ」は中身が児童館
    ("子ども中高生プラザ", "児童館・児童センター"),
    ("児童高齢者交流プラザ", "児童館・児童センター"),
    ("児童センター", "児童館・児童センター"),
    ("児童館", "児童館・児童センター"),
    # 港区の「子育てひろば あっぴぃ」「みなと子育て応援プラザ Pokke」「あい･ぽーと」
    ("あっぴぃ", "子育てひろば"),
    ("子育て応援プラザ", "子育てひろば"),
    ("あい･ぽーと", "子育てひろば"),
    ("あい・ぽーと", "子育てひろば"),
    ("学童クラブ", "学童クラブ"),
    ("放課後児童", "学童クラブ"),
    ("こども園", "認定こども園"),
    ("幼稚園", "幼稚園"),
    ("保育園", "保育所"),
    ("保育所", "保育所"),
    ("図書館", "図書館"),
    ("公民館", "公民館"),
    # 港区は「区民センター」「区民協働施設」が公民館にあたる。科学館・郷土歴史館も
    # 同じ「公共の建物」の印で出す
    ("区民センター", "区民センター"),
    ("区民協働施設", "区民センター"),
    ("いきいきプラザ", "区民センター"),
    ("科学館", "科学館・郷土歴史館"),
    ("郷土歴史館", "科学館・郷土歴史館"),
    ("伝統文化交流館", "科学館・郷土歴史館"),
    ("公園", "公園"),
]

# **今日、思い立って行けるか。**入園・登録が要る施設（保育所・幼稚園・学童クラブ）は
# 行き先にならないので、**取り込みの段階で落とす**（build() の最後）。画面にも出さない
DROPIN_TYPES = {
    "子育て支援センター", "子育てひろば", "子ども家庭支援センター", "児童館・児童センター",
    "公園", "図書館", "公民館", "区民センター", "科学館・郷土歴史館",
    "赤ちゃんほっとスペース", "赤ちゃんの駅",
    "おむつ替えのできるトイレ", "イベント",
}
ENROLL_TYPES = {
    "保育所", "幼稚園", "認定こども園", "学童クラブ", "小規模保育事業",
    "家庭的保育", "地域型保育事業", "認可保育施設",
}

DROPIN_NOTE = "予約なしで、その日に行けます"
ENROLL_NOTE = "入園・入会の手続きが要る施設です（見学は要問い合わせ）"


def facility_type(*texts, poi=None, given=None):
    """施設の種類を決める。POI コード → 種別欄 → 名前の順に見る。"""
    if poi and poi in POI_TYPES:
        return POI_TYPES[poi]
    given = (given or "").strip()
    if given and given not in ("0", "-", "不明"):
        for key, label in NAME_TYPES:
            if key in given:
                return label
        return given
    blob = " ".join(t for t in texts if t)
    for key, label in NAME_TYPES:
        if key in blob:
            return label
    return None


def is_dropin(ftype):
    if not ftype:
        return None
    if ftype in DROPIN_TYPES:
        return True
    for k in ENROLL_TYPES:
        if k in ftype:
            return False
    return True


# 種別 → (age_min, age_max)
AGE_RULES = [
    (("授乳", "おむつ", "赤ちゃん", "ほっとスペース"), (-9, 35)),
    (("子育て支援センター", "子育てひろば", "つどいの広場", "親と子のひろば",
      "子ども家庭支援センター"), (0, 71)),
    (("保育所", "保育園", "認定こども園", "こども園", "小規模保育", "家庭的保育"), (0, 71)),
    (("幼稚園",), (36, 71)),
    (("子育て支援", "子育てひろば", "つどいの広場", "地域子育て"), (0, 35)),
    (("児童館", "児童センター", "こどもセンター"), (0, 155)),
    (("学童", "放課後児童", "放課後子ども"), (72, 155)),
    (("公園", "緑地", "広場"), (0, 155)),
    (("図書館", "公民館", "文化会館"), (0, 155)),
]


def ages_for(*texts):
    blob = " ".join(t for t in texts if t)
    for keys, span in AGE_RULES:
        if any(k in blob for k in keys):
            return span
    return (0, 155)


def read_csv_bytes(raw, encodings=("utf-8-sig", "cp932", "utf-16")):
    for enc in encodings:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
        rows = list(csv.DictReader(io.StringIO(text)))
        if rows:
            return rows
    return []


def load_cache():
    return json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}


def geocode(address, cache):
    if not address:
        return None
    if address in cache:
        return cache[address]
    try:
        req = urllib.request.Request(GSI + urllib.parse.quote(address),
                                     headers={"User-Agent": "kosodate-navi"})
        with urllib.request.urlopen(req, timeout=30) as res:
            data = json.load(res)
        if data:
            lon, lat = data[0]["geometry"]["coordinates"]
            cache[address] = [lat, lon]
        else:
            cache[address] = None
    except Exception:                                # noqa: BLE001
        cache[address] = None
    time.sleep(0.2)
    return cache[address]


def base_spot(source, idx, name, lat, lon, category, **kw):
    ftype = kw.pop("facility_type", None) or facility_type(name, category)
    age_min, age_max = kw.pop("ages", None) or ages_for(ftype or "", category, name)
    spot = {
        "id": f"{source['id']}:{idx}",
        "name": (name or "").strip(),
        "lat": lat, "lon": lon,
        "category": category,
        "facility_type": ftype,
        "dropin": is_dropin(ftype),
        "visit_note": None,
        "age_min": age_min, "age_max": age_max,
        "open_time": None, "close_time": None, "closed_days": None,
        "fee_text": None, "fee_free": None,
        "stroller_ok": None, "nursing_room": None, "diaper_table": None, "hot_water": None,
        "indoor": None, "capacity": None, "note": None,
        "source_id": source["id"], "source_label": source.get("label"),
        "source_url": source.get("source_url") or source.get("url"),
        "checked_at": None,
    }
    spot.update(kw)
    if spot.get("visit_note") is None and spot.get("dropin") is not None:
        spot["visit_note"] = DROPIN_NOTE if spot["dropin"] else ENROLL_NOTE
    return spot


# --- 取り込み（kind ごと） ---

def from_nagareyama_facility(stage, source, cache):
    """公共施設所在地。**POI コードに施設の種類が入っている**ので、それを使う。

    1402a 保育所 / 1403a 児童館・児童センター / 1404a 学童クラブ /
    1406a 子育て支援センター / 1501a 幼稚園
    """
    raw = (stage.raw_dir / "spots" / f"{source['id']}.csv").read_bytes()
    rows = read_csv_bytes(raw)
    out = []
    for i, r in enumerate(rows):
        g = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        name, lat, lon = g.get("名称"), g.get("緯度"), g.get("経度")
        if not (name and lat and lon):
            continue
        ftype = facility_type(name, poi=g.get("POIコード"),
                              given=source.get("category") or g.get("小分類"))
        if source.get("category"):
            ftype = source["category"]
        cat = source.get("category") or ftype or source.get("label")
        spot = base_spot(source, i, name, float(lat), float(lon), cat, facility_type=ftype)
        if source.get("category") == "公園" or "公園" in (cat or ""):
            spot["indoor"] = False
            spot["fee_free"] = True
        out.append(spot)
    return out, 0


def from_nagareyama_hotspace(stage, source, cache):
    raw = (stage.raw_dir / "spots" / f"{source['id']}.csv").read_bytes()
    out = []
    for i, r in enumerate(read_csv_bytes(raw)):
        g = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if not (g.get("名称") and g.get("緯度")):
            continue
        spot = base_spot(source, i, g["名称"], float(g["緯度"]), float(g["経度"]),
                         "赤ちゃんほっとスペース", ages=(-9, 35),
                         facility_type="赤ちゃんほっとスペース")
        spot["nursing_room"] = True
        spot["diaper_table"] = True
        spot["indoor"] = True
        spot["fee_free"] = True
        out.append(spot)
    return out, 0


def from_nagareyama_toilet(stage, source, cache):
    raw = (stage.raw_dir / "spots" / f"{source['id']}.csv").read_bytes()
    out = []
    for i, r in enumerate(read_csv_bytes(raw)):
        g = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if g.get("乳幼児用設備設置トイレ有無") != "有":
            continue
        if not (g.get("緯度") and g.get("経度")):
            continue
        spot = base_spot(source, i, g.get("名称"), float(g["緯度"]), float(g["経度"]),
                         "おむつ替えのできるトイレ", ages=(-9, 35),
                         facility_type="おむつ替えのできるトイレ")
        spot["diaper_table"] = True
        spot["open_time"] = g.get("利用開始時間") or None
        spot["close_time"] = g.get("利用終了時間") or None
        spot["fee_free"] = True
        spot["note"] = g.get("利用可能時間特記事項") or None
        out.append(spot)
    return out, 0


def from_event(stage, source, cache):
    """推奨データセット「イベント一覧」。日付つきのスポットとして持つ。

    ※ 子育て向けのイベントを機械可読で出している自治体はまだ少ない。
       件数と最終更新を画面に出して、そのことも含めて見せる。
    """
    raw = (stage.raw_dir / "spots" / f"{source['id']}.csv").read_bytes()
    out, missing = [], 0
    for i, r in enumerate(read_csv_bytes(raw)):
        g = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        name = g.get("イベント名")
        if not name:
            continue
        lat, lon = g.get("緯度"), g.get("経度")
        if not (lat and lon):
            hit = geocode(g.get("住所"), cache) if g.get("住所") else None
            if not hit:
                missing += 1
                continue
            lat, lon = hit
        spot = base_spot(source, i, name, float(lat), float(lon), "イベント",
                         facility_type="イベント", ages=(0, 155))
        spot["event_start"] = g.get("開始日") or None
        spot["event_end"] = g.get("終了日") or g.get("開始日") or None
        spot["open_time"] = g.get("開始時間") or None
        spot["close_time"] = g.get("終了時間") or None
        spot["note"] = " ".join(x for x in (g.get("説明"), g.get("備考")) if x) or None
        spot["place_name"] = g.get("開催場所名称") or None
        spot["indoor"] = None
        if g.get("URL"):
            # **イベント一覧には市のページの URL が入っている。**
            # これが「中身を web で確かめられる」印になる（URL の無い回は地図に出さない）
            spot["source_url"] = g["URL"]
            spot["official_url"] = g["URL"]
            spot["official_label"] = f"{stage.name} イベントのページ"
        out.append(spot)
    return out, missing


def from_local(stage, source, cache):
    path = stages_mod.REPO_ROOT / "data" / "spots" / source["file"]
    if not path.exists():
        return [], 0
    out = []
    for i, r in enumerate(read_csv_bytes(path.read_bytes())):
        if not (r.get("name") and r.get("lat")):
            continue
        cat = r.get("category") or source.get("label")
        spot = base_spot(source, i, r["name"], float(r["lat"]), float(r["lon"]), cat,
                         facility_type=r.get("facility_type")
                         or facility_type(r["name"], given=cat))
        for k in ("facility_type", "visit_note", "event_start", "event_end", "place_name"):
            if r.get(k):
                spot[k] = r[k]
        if r.get("facility_type"):
            spot["dropin"] = is_dropin(r["facility_type"])
            spot["visit_note"] = r.get("visit_note") or (
                DROPIN_NOTE if spot["dropin"] else ENROLL_NOTE)
        if r.get("dropin"):
            spot["dropin"] = r["dropin"].lower() in ("1", "true", "yes")
        for k in ("age_min", "age_max"):
            if r.get(k):
                spot[k] = int(r[k])
        # 公式ページ・概要・広さも自前整備の CSV から読む（**都立公園のように、
        # 市区町村のオープンデータにも市のホームページにも出てこない行き先**がある）
        for k in ("official_url", "official_label", "summary", "playground",
                  "open_hours", "city", "park_rank"):
            if r.get(k):
                spot[k] = r[k]
        if r.get("area_m2"):
            spot["area_m2"] = int(float(r["area_m2"]))
        if (r.get("toilet") or "").lower() in ("1", "true", "有", "○"):
            spot["toilet"] = True
        for k in ("open_time", "close_time", "closed_days", "fee_text", "note",
                  "source_url", "checked_at"):
            if r.get(k):
                spot[k] = r[k]
        for k in ("fee_free", "stroller_ok", "nursing_room", "diaper_table",
                  "hot_water", "indoor"):
            if r.get(k):
                spot[k] = r[k].lower() in ("1", "true", "yes", "有", "○")
        out.append(spot)
    return out, 0


# --- 港区（2026-09-27 追加） ---
#
# **港区の施設情報オープンデータは、この作品にとって特別に都合がよい。**
# 流山市では「名称・住所・座標・電話」しか無く、中身（遊具・利用時間・公式ページ）は
# 市のホームページを 1 件ずつ当たって自前整備するしかなかった。港区は
# **施設ごとの公式ページ URL（`ファイルパス`）が 99〜100% 入っている**ので、
# 「web で中身を確かめたスポットだけ出す」という決まりを**オープンデータだけで満たせる**。
# 公園は `施設の概要` が 71%、区民センターは `開館時間` が 88% 埋まっている。
#
# 5 つのデータセットが**同じ列**（ページタイトル／第2分類／ファイルパス／所在地／
# 開館時間／休業日／施設の概要／紹介文／電車／バス／緯度／経度）で出ているので、
# 取り込みは 1 つで足りる。何を採るかは設定（`sub` / `include_path` / `exclude_name`）で決める。

HOUR_RE = re.compile(r"午(前|後)\s*(\d{1,2})時(?:\s*(\d{1,2})分)?")
# **港区の公園は、広さが「施設の概要」の文章の中に書いてある**
# （「面積:4,542.41平方メートル」「面積（㎡）：574.68」）。列にはなっていない。
# 162 件中 129 件から読み取れるので、OSM のポリゴンを測るより正確で件数も多い
AREA_RE = re.compile(r"面積[（(]?㎡?[)）]?\s*[:：]\s*([0-9,]+(?:\.[0-9]+)?)\s*(?:平方メートル|㎡)?")


def _minato_hours(text):
    """「午前9時から午後5時まで」を (09:00, 17:00) にする。読めなければ (None, None)。

    港区の `開館時間` は文章なので、**読めたときだけ**時刻にする
    （読めなかったものは `open_hours` に文のまま残す）。
    """
    if not text:
        return None, None
    got = []
    for ampm, h, m in HOUR_RE.findall(text.replace("　", " ")):
        h = int(h) % 12 + (12 if ampm == "後" else 0)
        got.append(f"{h:02d}:{int(m or 0):02d}")
        if len(got) == 2:
            break
    return (got[0], got[1]) if len(got) == 2 else (None, None)


def from_minato_facility(stage, source, cache):
    """港区の施設情報オープンデータ（公園・子どもの施設・図書館・区民センター）。"""
    raw = (stage.raw_dir / "spots" / f"{source['id']}.csv").read_bytes()
    keep_sub = source.get("sub")
    inc = source.get("include_path")
    exc = source.get("exclude_name") or []
    out, dropped = [], 0
    for i, r in enumerate(read_csv_bytes(raw)):
        g = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        name, lat, lon = g.get("ページタイトル"), g.get("緯度"), g.get("経度")
        if not (name and lat and lon):
            dropped += 1
            continue
        path = g.get("ファイルパス") or ""
        if keep_sub and g.get("第2分類") not in keep_sub:
            continue
        if inc and not any(k in path for k in inc):
            continue
        if any(k in name for k in exc):          # 学童クラブなど、入会が要るもの
            continue
        cat = source.get("category") or g.get("第2分類") or source.get("label")
        # **第2分類より名前を先に見る。** 第2分類は「子育てひろば・子育て関連施設」のように
        # 保育園も児童館も一緒くたなので、施設の種類は名前から決めたほうが正しい
        ftype = source.get("category") or facility_type(name) \
            or facility_type(name, given=g.get("第2分類"))
        spot = base_spot(source, i, name, float(lat), float(lon), cat, facility_type=ftype)
        if path.startswith("http"):
            spot["official_url"] = path
            spot["official_label"] = "港区 施設案内"
        summary = g.get("施設の概要") or g.get("紹介文")
        if summary:
            spot["summary"] = " ".join(summary.split())[:220]
        m = AREA_RE.search(" ".join(((g.get("施設の概要") or "")
                                     + " " + (g.get("紹介文") or "")).split()))
        if m:
            spot["area_m2"] = int(float(m.group(1).replace(",", "")))
        opened, closed = _minato_hours(g.get("開館時間"))
        spot["open_time"], spot["close_time"] = opened, closed
        if g.get("開館時間") and not opened:
            spot["open_hours"] = " ".join(g["開館時間"].split())
        if g.get("休業日"):
            spot["closed_days"] = " ".join(g["休業日"].split())
        if source.get("category") == "公園":
            spot["indoor"] = False
            spot["fee_free"] = True
        out.append(spot)
    return out, dropped


def from_hokonavi_toilet(stage, source, cache):
    """ほこナビのバリアフリー施設等データ（東京都・車椅子使用者対応トイレ）。

    **おむつ交換台のあるものだけ**を「おむつ替えのできるトイレ」として取り込む。
    都全体で 5,692 施設・うちおむつ交換台 3,544 件あり、`lgCode` で区市町村を絞る
    （港区 131032 は 163 施設・おむつ交換台 136）。
    流山市には「おむつ替えのできるトイレ」が 2 件しか無かったので、桁が違う。
    """
    dest = stage.raw_dir / "spots" / f"{source['id']}.zip"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(source["url"], headers={"User-Agent": "kosodate-navi"})
        with urllib.request.urlopen(req, timeout=300) as res:
            dest.write_bytes(res.read())
    lg = str(source["lg_code"])
    with zipfile.ZipFile(dest) as z:
        fac_name = [n for n in z.namelist() if n.endswith("facility.csv")][0]
        rows = read_csv_bytes(z.read(fac_name))
        time_name = [n for n in z.namelist() if n.endswith("time.csv")]
        # **time.csv だけ Shift_JIS**（ほかは UTF-8）。read_csv_bytes が吸収する
        hours = {}
        for t in (read_csv_bytes(z.read(time_name[0])) if time_name else []):
            fid = (t.get("facilId") or "").strip()
            if fid and fid not in hours:
                hours[fid] = t
    out, dropped = [], 0
    for i, r in enumerate(rows):
        g = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if g.get("lgCode") != lg:
            continue
        if g.get("t_dTable") not in ("1", "true", "TRUE"):   # おむつ交換台が無いものは出さない
            dropped += 1
            continue
        if not (g.get("latitude") and g.get("longitude")):
            dropped += 1
            continue
        spot = base_spot(source, i, g.get("name"), float(g["latitude"]), float(g["longitude"]),
                         "おむつ替えのできるトイレ", ages=(-9, 35),
                         facility_type="おむつ替えのできるトイレ")
        spot["diaper_table"] = True
        spot["fee_free"] = True
        spot["indoor"] = True
        if g.get("t_bChair") in ("1", "true", "TRUE"):
            spot["note"] = "ベビーチェアもあります"
        t = hours.get((g.get("facilId") or "").strip())
        if t:
            spot["open_time"] = (t.get("t_openTime") or "").strip()[:5] or None
            spot["close_time"] = (t.get("t_closeTime") or "").strip()[:5] or None
        spot["official_url"] = source.get("source_url")
        spot["official_label"] = "ほこナビ バリアフリー施設等データ（東京都）"
        spot["summary"] = ("車椅子使用者対応トイレで、**おむつ交換台があります**。"
                           "東京都がバリアフリー情報として公開しているものです。")
        out.append(spot)
    return out, dropped


LOADERS = {
    "nagareyama_facility": from_nagareyama_facility,
    "minato_facility": from_minato_facility,
    "hokonavi_toilet": from_hokonavi_toilet,
    "nagareyama_hotspace": from_nagareyama_hotspace,
    "nagareyama_toilet": from_nagareyama_toilet,
    "municipal_event": from_event,
    "local": from_local,
}


# 出どころによって全角と半角、「ケ」と「ヶ」がばらばら（「流山おおたかの森Ｓ・Ｃ」と
# 「流山おおたかの森S・C」が別の場所になってしまう）
ZEN = "".join(chr(0xFF01 + i) for i in range(94))
HAN = "".join(chr(0x21 + i) for i in range(94))
TO_HAN = str.maketrans(ZEN, HAN)


def norm_name(name):
    out = (name or "").replace("　", "").replace(" ", "").translate(TO_HAN)
    # **同じ場所に付く注記は出どころごとに違う**（ほこナビは「西戸山公園（東側）」、
    # 東京都の公園データは「西戸山公園」）。括弧の中は落としてから突き合わせる
    out = re.sub(r"[（(][^）)]*[）)]", "", out)
    out = out.replace("ケ", "ヶ").replace("・", "")
    # **自治体名の肩書きは出どころによって付いたり付かなかったりする。**
    # 同じ公園が「流山市立〇〇公園」と「〇〇公園」で 2 件に見えてしまうので落とす
    for word in ("子育て支援センター", "地域子育て支援拠点", "ルーム",
                 "流山市立", "流山市", "市立", "区立"):
        out = out.replace(word, "")
    return out


# 施設の種類の優先順。同じ場所が複数の出どころに出てきたとき、どれを見出しにするか。
# 「児童センターで、授乳もできる」なら見出しは児童センターにして、授乳は設備として残す。
TYPE_PRIORITY = [
    "子育て支援センター", "子育てひろば", "子ども家庭支援センター", "児童館・児童センター",
    "図書館", "公民館", "イベント", "公園", "赤ちゃんほっとスペース", "赤ちゃんの駅",
    "おむつ替えのできるトイレ",
]


def merge_spot(base, other):
    """同じ場所の 2 件を 1 件にまとめる。**消さずに統合する**。"""
    for key, value in other.items():
        if key in ("id", "source_id", "source_label", "source_url", "checked_at", "also_in"):
            continue
        if base.get(key) in (None, "", []) and value not in (None, "", []):
            base[key] = value
    # 設備は「どちらかにあれば有り」
    for key in ("nursing_room", "diaper_table", "hot_water", "stroller_ok", "fee_free"):
        if other.get(key):
            base[key] = True
    # 見出しにする種類は優先順で決める
    types = [t for t in (base.get("facility_type"), other.get("facility_type")) if t]
    if types:
        def rank(t):
            return TYPE_PRIORITY.index(t) if t in TYPE_PRIORITY else len(TYPE_PRIORITY)
        head = sorted(types, key=rank)[0]
        # **名前も、見出しになった側のものを使う。** トイレのデータは
        # 「〇〇市立△△公園」、公園のオープンデータは「△△公園」で、
        # 公園として見せるなら公園側の名前のほうが通りがよい
        if head != base.get("facility_type") and other.get("name"):
            base["name"] = other["name"]
        base["facility_type"] = head
        base["dropin"] = is_dropin(base["facility_type"])
        base["visit_note"] = DROPIN_NOTE if base["dropin"] else ENROLL_NOTE
    extra = {t for t in types if t != base["facility_type"]}
    if extra:
        base["also_types"] = sorted(set(base.get("also_types", [])) | extra)
    base.setdefault("also_in", []).append(other["source_id"])
    base.setdefault("also_sources", []).append(
        {"label": other.get("source_label"), "url": other.get("source_url")})
    # 年齢は広いほうに寄せる（児童センターで授乳もできる、のような場合）
    base["age_min"] = min(base["age_min"], other["age_min"])
    base["age_max"] = max(base["age_max"], other["age_max"])
    return base


def dedupe(spots):
    """同じ施設が複数の出どころに出てくるので、**中身を統合して 1 件にする**。

    自前整備（利用時間・料金・予約の要否つき）を土台にし、足りない欄を他から埋める。
    """
    def richness(s):
        keys = ("open_time", "close_time", "fee_text", "visit_note", "note",
                "nursing_room", "diaper_table", "capacity")
        return sum(1 for k in keys if s.get(k)) + (2 if s["source_id"].endswith("_local") else 0)

    # 公園は同名が離れて存在しうるので近いものだけ、施設は住所ジオコーディングの
    # ずれがあるので広めに見る（同じ名前＋同じ市なら同一とみなしてよい）
    def radius(spot):
        return 0.004 if (spot.get("facility_type") or "") in ("公園", "イベント") else 0.014

    kept = []
    for spot in sorted(spots, key=richness, reverse=True):
        key = norm_name(spot["name"])
        if not key:
            kept.append(spot)
            continue
        dup = None
        for other in kept:
            if norm_name(other["name"]) != key:
                continue
            d = ((other["lat"] - spot["lat"]) ** 2 + (other["lon"] - spot["lon"]) ** 2) ** 0.5
            if d < max(radius(spot), radius(other)):
                dup = other
                break
        if dup is None:
            kept.append(spot)
        else:
            # 座標は自治体のオープンデータ側が正確（自前分は住所からの推定）
            if dup["source_id"].endswith("_local") and not spot["source_id"].endswith("_local"):
                dup["lat"], dup["lon"] = spot["lat"], spot["lon"]
            merge_spot(dup, spot)
    return kept


# 自前整備（scripts/build_spot_details.py が作る）の列 → スポットの列
DETAIL_TEXT = ["summary", "official_url", "official_label", "city_page_url", "city_category",
               "where", "playground", "area", "barrier_free", "station", "tel"]
DETAIL_FLAGS = ["nursing_room", "diaper_table", "hot_water", "stroller_lend", "toilet"]


def apply_details(stage, spots):
    """**概要と公式ページ**を付ける。市のオープンデータには両方とも入っていないので、
    市の施設ページから自前でそろえた `data/spots/<stage>_details.csv` を重ねる。

    設備の欄は **True だけを書き込む**。「載っていない＝無い」ではないので、
    False で上書きするとデータを出していない施設ほど不利になる。
    """
    path = stages_mod.REPO_ROOT / "data" / "spots" / f"{stage.id}_details.csv"
    if not path.exists():
        return spots, {"detailed": 0, "closed": 0, "closed_names": []}
    rows = read_csv_bytes(path.read_bytes())
    by_name = {}
    for row in rows:
        by_name.setdefault(norm_name(row.get("name")), []).append(row)

    detailed, closed = 0, []
    kept = []
    for spot in spots:
        row = None
        for cand in by_name.get(norm_name(spot["name"]), []):
            try:
                far = ((float(cand["lat"]) - spot["lat"]) ** 2
                       + (float(cand["lon"]) - spot["lon"]) ** 2) ** 0.5 > 0.02
            except (TypeError, ValueError):
                far = False
            if not far:
                row = cand
                break
        if row is None:
            kept.append(spot)
            continue
        if (row.get("status") or "").strip() == "closed":
            # **廃止された施設がオープンデータに残っている。**地図には出さない
            closed.append({"name": spot["name"], "note": row.get("status_note") or "",
                           "source_url": row.get("official_url") or ""})
            continue
        detailed += 1
        for key in DETAIL_TEXT:
            if row.get(key):
                spot[key] = row[key]
        for key in DETAIL_FLAGS:
            if (row.get(key) or "").strip() in ("1", "true", "有"):
                spot["stroller_ok" if key == "stroller_lend" else key] = True
        if row.get("checked_at"):
            spot["detail_checked_at"] = row["checked_at"]
        if row.get("walk_min"):
            spot["walk_min"] = int(row["walk_min"])
        if row.get("open_hours") and not spot.get("open_time"):
            spot["open_hours"] = row["open_hours"]
        if row.get("closed_days") and not spot.get("closed_days"):
            spot["closed_days"] = row["closed_days"]
        hint = row.get("facility_type_hint")
        # **トイレを公園に昇格させない。** 「総合運動公園（アスレチック広場内）」は
        # 公園の中にあるおむつ替えトイレで、市のページは公園のページに当たる。
        # そのまま種類を公園に変えると、同じ公園が 2 つの印になってしまう
        if hint == "公園" and (spot.get("facility_type") or "").endswith("トイレ"):
            hint = None
        if hint and hint != spot.get("facility_type"):
            # 市のページの置き場所のほうが具体的（ほっとスペース → 図書館）
            def rank(t):
                return TYPE_PRIORITY.index(t) if t in TYPE_PRIORITY else len(TYPE_PRIORITY)
            if rank(hint) < rank(spot.get("facility_type")):
                spot.setdefault("also_types", [])
                if spot.get("facility_type"):
                    spot["also_types"] = sorted(set(spot["also_types"]) | {spot["facility_type"]})
                spot["facility_type"] = hint
                span = ages_for(hint)
                spot["age_min"] = min(spot["age_min"], span[0])
                spot["age_max"] = max(spot["age_max"], span[1])
                spot["dropin"] = is_dropin(hint)
                spot["visit_note"] = DROPIN_NOTE if spot["dropin"] else ENROLL_NOTE
        kept.append(spot)
    return kept, {"detailed": detailed, "closed": len(closed), "closed_names": closed}


# --- 公園の選定 ---
#
# **公園が多すぎる**（流山市 429 件・市外 1,713 件）。遠くの小さな公園にわざわざ行く人は
# いないので、広さ・遊具・設備で 3 つに分ける。分け方は `backend/spots.py` に置いて
# サーバと共有し、ここでは**広さ（`area_m2`）を付けるところまで**をやる。
# 広さは OSM のポリゴン（build_osm_spots.py が測ったもの）から取る。


def load_park_polygons(stage):
    """build_osm_spots.py が書き出した、市内の公園ポリゴン。"""
    path = OUT_DIR / f"{stage.id}_park_polygons.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("polygons", [])


# OSM の公園名に付く運営者の肩書き。市の一覧には入っていないので落として突き合わせる
PARK_NAME_PREFIXES = ("流山市立", "流山市", "都立", "県立", "千葉県立",
                      "東京都立", "区立", "市立")


def norm_park_name(name):
    out = (name or "").replace("　", "").replace(" ", "").translate(TO_HAN)
    out = out.replace("ケ", "ヶ")
    for word in PARK_NAME_PREFIXES:
        out = out.replace(word, "")
    return out


def _in_ring(lat, lon, ring):
    ok = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            ok = not ok
    return ok


def apply_park_size(stage, spots):
    """公園の点を包む OSM のポリゴンを探して、広さ（m²）を付ける。

    **1 つのポリゴンに市の公園が 2 つ以上入っていたら、名前が一致するものにしか付けない。**
    「東深井地区公園」のポリゴンの中に 1〜4 号緑地が並んでいて、全部を 9.75ha に
    してしまうため。市のページに広さが書いてあるとき（「約1.2ヘクタール」）はそちらを優先する。
    """
    polys = load_park_polygons(stage)
    parks = [s for s in spots if (s.get("facility_type") or "") in spots_mod.PARK_TYPES]
    # 市区町村のページに書いてある広さ。**こちらが最優先**（OSM のポリゴンは
    # 公園の外側の緑地まで含むことがあるので、自治体の数字があるならそれを使う）
    from_page = 0
    for s in parks:
        text = (s.get("area") or "").replace("約", "").replace(",", "").strip()
        m = re.match(r"([\d.]+)\s*(ヘクタール|平方メートル|㎡|m2)", text)
        if m:
            try:
                value = float(m.group(1))
            except ValueError:
                continue
            s["area_m2"] = int(value * 10000) if m.group(2) == "ヘクタール" else int(value)
            from_page += 1
    # ポリゴンとの対応。まず「どの公園がどのポリゴンに入るか」を数える
    hit = {}
    for i, s in enumerate(parks):
        best = None
        for j, pg in enumerate(polys):
            x1, y1, x2, y2 = pg["bbox"]
            if x1 <= s["lon"] <= x2 and y1 <= s["lat"] <= y2 \
                    and _in_ring(s["lat"], s["lon"], pg["ring"]):
                if best is None or pg["area_m2"] < polys[best]["area_m2"]:
                    best = j
        if best is not None:
            hit[i] = best
    count = {}
    for j in hit.values():
        count[j] = count.get(j, 0) + 1
    from_osm = 0
    for i, j in hit.items():
        if parks[i].get("area_m2"):
            continue
        if count[j] > 1 and norm_park_name(polys[j]["name"]) != norm_park_name(parks[i]["name"]):
            continue                      # 大きい公園の中に小さい公園が並んでいる
        parks[i]["area_m2"] = polys[j]["area_m2"]
        from_osm += 1
    # 点がポリゴンの外にあることがある（住所の代表点や入口に打たれている）。
    # **名前が一致して 300m 以内**ならそのポリゴンの広さを使う
    by_name = {}
    for pg in polys:
        if pg["name"]:
            by_name.setdefault(norm_park_name(pg["name"]), []).append(pg)
    from_name = 0
    for s in parks:
        if s.get("area_m2"):
            continue
        for pg in by_name.get(norm_park_name(s["name"]), []):
            x1, y1, x2, y2 = pg["bbox"]
            clat, clon = (y1 + y2) / 2, (x1 + x2) / 2
            if abs(s["lat"] - clat) * 111000 < 300 and abs(s["lon"] - clon) * 91000 < 300:
                s["area_m2"] = pg["area_m2"]
                from_name += 1
                break
    ranks = {}
    for s in parks:
        ranks[spots_mod.park_rank(s)] = ranks.get(spots_mod.park_rank(s), 0) + 1
    return {"parks": len(parks), "area_from_page": from_page,
            "area_from_osm": from_osm + from_name, "area_by_name": from_name,
            "sized": sum(1 for s in parks if s.get("area_m2")),
            "ranks": sorted(ranks.items(), key=lambda x: -x[1])}


def drop_enrollment(spots):
    """**入園・入会の手続きが要る施設を落とす。**

    保育所・幼稚園・学童クラブは「今日どこかに行きたい」の行き先にならない。
    以前は画面の切り替えで出せるようにしていたが、出す用途が無いので
    **データそのものから外す**ことにした。落とした件数だけ種類別に残して見せる。
    """
    kept = [s for s in spots if s.get("dropin") is not False]
    dropped = {}
    for s in spots:
        if s.get("dropin") is False:
            k = s.get("facility_type") or "その他"
            dropped[k] = dropped.get(k, 0) + 1
    return kept, sorted(dropped.items(), key=lambda x: -x[1])


def build(stage):
    cache = load_cache()
    spots, sources = [], []
    excluded = {}
    for source in stage.spots:
        loader = LOADERS.get(source["kind"])
        if not loader:
            continue
        try:
            got, missing = loader(stage, source, cache)
        except FileNotFoundError:
            print(f"  ⏭  {source['id']}: 元データがありません")
            continue
        got, dropped = drop_enrollment(got)
        for k, n in dropped:
            excluded[k] = excluded.get(k, 0) + n
        spots.extend(got)
        sources.append({"id": source["id"], "label": source.get("label"),
                        "kind": source["kind"], "count": len(got),
                        "dropin": sum(1 for s in got if s.get("dropin")),
                        "enrollment_excluded": sum(n for _, n in dropped),
                        "no_location": missing,
                        "source_url": source.get("source_url") or source.get("url")})
        note = f"（座標が無くて出せないもの {missing} 件）" if missing else ""
        skip = f"／入園・入会が要るので外したもの {sum(n for _, n in dropped)} 件" if dropped else ""
        print(f"  {source['id']}: {len(got)} 件{note}{skip}")
    before = len(spots)
    spots = dedupe(spots)
    if before != len(spots):
        print(f"  重複をまとめた: {before} → {len(spots)} 件")
    spots, detail_stats = apply_details(stage, spots)
    park_stats = apply_park_size(stage, spots)
    detail_stats["parks"] = park_stats
    if park_stats["parks"]:
        print(f"  公園 {park_stats['parks']} 件のうち {park_stats['sized']} 件に広さ: "
              f"市のページ {park_stats['area_from_page']} 件／"
              f"OSM のポリゴン {park_stats['area_from_osm']} 件"
              f"（うち名前で照合 {park_stats['area_by_name']} 件）")
        print("  公園の選定: " + "／".join(f"{k} {v}" for k, v in park_stats["ranks"]))
    # 市のページの置き場所で種類が変わることがある（→ 入園が要る施設になったら落とす）
    spots, dropped = drop_enrollment(spots)
    for k, n in dropped:
        excluded[k] = excluded.get(k, 0) + n
    detail_stats["enrollment_excluded"] = sum(excluded.values())
    detail_stats["enrollment_types"] = sorted(excluded.items(), key=lambda x: -x[1])
    if excluded:
        print("  入園・入会が要るので取り込まない: "
              + "／".join(f"{k} {n}" for k, n in detail_stats["enrollment_types"]))
    if detail_stats["detailed"]:
        print(f"  概要と公式ページを付けた: {detail_stats['detailed']} 件")
    if detail_stats["closed"]:
        print(f"  廃止済みとして出さない: {detail_stats['closed']} 件 "
              + "／".join(c["name"] for c in detail_stats["closed_names"]))
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{stage.id}.json").write_text(
        json.dumps({"stage": stage.id, "sources": sources, "spots": spots,
                    "details": detail_stats}, ensure_ascii=False), encoding="utf-8")
    print(f"  ✅ 合計 {len(spots)} 件")


def main():
    all_stages = stages_mod.load_all()
    targets = [all_stages[sys.argv[1]]] if len(sys.argv) > 1 else list(all_stages.values())
    for stage in targets:
        print(f"== {stage.name} ==")
        build(stage)


if __name__ == "__main__":
    main()
