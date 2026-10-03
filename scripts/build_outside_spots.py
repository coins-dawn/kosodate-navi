#!/usr/bin/env python3.9
"""**舞台の外**の行き先を作る。出どころは自前整備の CSV（1 件ずつ web で確かめたもの）。

    python3.9 scripts/build_outside_spots.py [stage_id]

入力: data/spots/<stage>_outside.csv
出力: data/spots/generated/<stage>_outside.json

到達圏は市の境で止まらない。自宅から 40 分で柏市や松戸市の公園に着けるなら、
その公園は行き先の候補になる。ところが**スポットのオープンデータは市ごと**なので、
隣の市は空白になる。

**以前はそこを OpenStreetMap で埋めていた**（流山市のまわりだけで 2,097 件）。
やめた理由は 2 つ。

1. OSM にあるのは名前と形だけで、**行き先を選ぶのに要る中身（遊具・トイレ・広さ・
   開いている時間・公式ページ）が無い**。名前しか分からない公園を 1,700 件出しても選べない。
2. 件数が多すぎて地図が公園で埋まる。

代わりに、**隣の市が自分のホームページで挙げているもの**を 1 件ずつ当たって表にした。
**自治体が紹介している＝わざわざ行く価値がある**という選び方で、しかも中身と公式ページが必ず付く。

- **公園**: 松戸市「まつどDE子育て・市内の公園」、柏市の施設案内、東京都公園協会「公園へ行こう！」など
- **子育て支援センター・児童館**（2026-09-27 追加）: 柏市の施設案内、松戸市「まつどDE子育て」の
  おやこDE広場／子育て支援センター／児童館・こども館の一覧
- **授乳・おむつ替え**（2026-09-27 追加）: 柏市「あかちゃんほっとステーション」、
  松戸市「赤ちゃんぽけっと」。**流山市の「赤ちゃんほっとスペース」と同じ性格の制度**で、
  市ごとに呼び名が違うだけなので、`facility_type` は「授乳・おむつ替え」にそろえた
  （もとの呼び名は `official_label` と `summary` に残す）

座標は国土地理院のジオコーダで住所から引く（結果は data/spots/geocode_cache.json）。
"""
import csv
import io
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import stages as stages_mod  # noqa: E402
from backend import spots as spots_mod    # noqa: E402

REPO = stages_mod.REPO_ROOT
OUT_DIR = REPO / "data" / "spots" / "generated"
CACHE = REPO / "data" / "spots" / "geocode_cache.json"
GSI = "https://msearch.gsi.go.jp/address-search/AddressSearch?q="

FLAGS = ("toilet", "diaper_table", "nursing_room", "stroller_ok", "fee_free")
TEXTS = ("summary", "official_url", "official_label", "playground", "open_hours",
         "closed_days", "fee_text", "address", "source_note", "checked_at")


def geocode(address, cache):
    if address in cache:
        return cache[address]
    req = urllib.request.Request(GSI + urllib.parse.quote(address),
                                 headers={"User-Agent": "kosodate-navi"})
    with urllib.request.urlopen(req, timeout=30) as res:
        data = json.load(res)
    cache[address] = ([data[0]["geometry"]["coordinates"][1],
                       data[0]["geometry"]["coordinates"][0]] if data else None)
    time.sleep(0.3)
    return cache[address]


def from_dataset(stage, source, start_id):
    """**区市町村をまたいで配られているデータセット**から、舞台の外の行き先を作る。

    自前整備の CSV（1 件ずつ web で確かめたもの）とは出どころが違うが、
    **配っている側がすでに区市町村をまたいでいる**ので、1 件ずつ当たり直す必要がない。
    いまのところ `hokonavi_toilet`（ほこナビのバリアフリー施設等データ・東京都）だけ。

    港区の舞台では、半径 6km の円が品川区・中央区・江東区・渋谷区・千代田区・目黒区に
    かかる。同じ 1 つのデータセットに全部入っているので、`lg_codes` を並べるだけで済む。
    """
    import zipfile
    kind = source.get("kind")
    if kind != "hokonavi_toilet":
        raise SystemExit(f"  ! 未知の outside の kind: {kind}")
    dest = stage.raw_dir / "spots" / f"{source['id']}.zip"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(source["url"], headers={"User-Agent": "kosodate-navi"})
        with urllib.request.urlopen(req, timeout=300) as res:
            dest.write_bytes(res.read())
    codes = {str(k): v for k, v in (source.get("lg_codes") or {}).items()}
    with zipfile.ZipFile(dest) as z:
        name = [n for n in z.namelist() if n.endswith("facility.csv")][0]
        rows = list(csv.DictReader(io.StringIO(z.read(name).decode("utf-8-sig"))))
    out = []
    for i, r in enumerate(rows):
        g = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        city = codes.get(g.get("lgCode"))
        if not city:
            continue
        if g.get("t_dTable") not in ("1", "true", "TRUE"):   # おむつ交換台が無いものは出さない
            continue
        if not (g.get("latitude") and g.get("longitude")):
            continue
        out.append({
            "id": f"outside:{start_id + i}", "name": g.get("name"),
            "lat": float(g["latitude"]), "lon": float(g["longitude"]),
            "category": "おむつ替えのできるトイレ",
            "facility_type": "おむつ替えのできるトイレ",
            "dropin": True, "visit_note": "予約なしで、その日に行けます",
            "age_min": -9, "age_max": 35,
            "open_time": None, "close_time": None, "closed_days": None,
            "fee_text": None, "fee_free": True,
            "stroller_ok": None, "nursing_room": None,
            "diaper_table": True,
            "hot_water": None, "indoor": True, "capacity": None,
            "note": "ベビーチェアもあります" if g.get("t_bChair") in ("1", "true", "TRUE") else None,
            "city": city, "outside": True,
            "source_id": source["id"], "source_label": source.get("label"),
            "source_url": source.get("source_url"),
            "official_url": source.get("source_url"),
            "official_label": "ほこナビ バリアフリー施設等データ（東京都）",
            "summary": "車椅子使用者対応トイレで、おむつ交換台があります。"
                       "東京都がバリアフリー情報として公開しているものです。",
            "checked_at": source.get("checked_at"),
        })
    return out


def build(stage):
    path = REPO / "data" / "spots" / f"{stage.id}_outside.csv"
    sources = getattr(stage, "outside_sources", [])
    if not path.exists() and not sources:
        print(f"  ⏭  {stage.name}: {path.name} も outside の設定もありません")
        return
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig"))) if path.exists() else []
    # **同じ行き先を 2 回書いてしまうのを止める。** 出どころの違うページ
    # （施設案内と赤ちゃんの駅の一覧など）から拾うと、同じ場所が二重に入りやすい。
    # そのときは 1 行にまとめて、`source_note` に両方の出どころを書くこと
    seen = {}
    for row in rows:
        key = (row.get("name", "").strip(), row.get("city", "").strip())
        if key in seen:
            raise SystemExit(f"  ! {path.name} に同じ行き先が 2 回あります: {key[1]}{key[0]}"
                             f"（{seen[key] + 2} 行目と {rows.index(row) + 2} 行目）。"
                             "1 行にまとめてください")
        seen[key] = rows.index(row)
    spots, cities, missing = [], {}, []
    for i, row in enumerate(rows):
        lat, lon = row.get("lat"), row.get("lon")
        if not (lat and lon):
            got = geocode(row["address"], cache)
            if not got:
                missing.append(row["name"])
                continue
            lat, lon = got
        lat, lon = float(lat), float(lon)
        ftype = row.get("facility_type") or "公園"
        spot = {
            "id": f"outside:{i}", "name": row["name"].strip(),
            "lat": lat, "lon": lon,
            "category": ftype, "facility_type": ftype,
            "dropin": True, "visit_note": "予約なしで、その日に行けます",
            "age_min": 0, "age_max": 155,
            # 利用時間は CSV に書いてあれば使う（「10:00〜16:00」のように幅で言えるもの）。
            # 「開館時間内」のように幅で言えないものは open_hours に文のまま入れる
            "open_time": (row.get("open_time") or "").strip() or None,
            "close_time": (row.get("close_time") or "").strip() or None,
            "closed_days": None,
            "fee_text": None, "fee_free": None,
            "stroller_ok": None, "nursing_room": None, "diaper_table": None,
            "hot_water": None, "indoor": None, "capacity": None, "note": None,
            "city": row.get("city") or "", "outside": True,
            "source_id": "outside", "source_label": "自前整備（市外の行き先）",
            "source_url": row.get("official_url") or "",
        }
        for key in TEXTS:
            if row.get(key):
                spot[key] = row[key].strip()
        for key in FLAGS:
            if (row.get(key) or "").strip() in ("1", "true", "有", "○"):
                spot[key] = True
        if row.get("area_m2"):
            spot["area_m2"] = int(float(row["area_m2"]))
        # **階級は CSV に書いてあるものを尊重する。** 自治体が代表的な公園として
        # 挙げているなら、広さが分からなくても「わざわざ行く公園」でよい
        if row.get("park_rank"):
            spot["park_rank"] = row["park_rank"].strip()
        if row.get("closed_days"):
            spot["closed_days"] = row["closed_days"].strip()
        spots.append(spot)
        cities[spot["city"]] = cities.get(spot["city"], 0) + 1
    # データセットから作る分（自前整備の CSV とは別口）
    for source in sources:
        got = from_dataset(stage, source, len(rows) + 1000)
        for spot in got:
            spots.append(spot)
            cities[spot["city"]] = cities.get(spot["city"], 0) + 1
        print(f"    {source.get('label') or source['id']}: {len(got)} 件")
    # **OSM の候補が何件あって、そこから何件を選んだか**を残す（画面で見せる）。
    # ただしこれは**公園を選ぶときの話**なので、自前整備に公園が 1 件も無い舞台では出さない
    # （港区の自前整備は児童館だけなので、OSM の候補件数を並べると話が食い違う）
    cand = OUT_DIR / f"{stage.id}_osm_candidates.json"
    has_park = any((r.get("facility_type") or "公園") == "公園" for r in rows)
    candidates = (len(json.loads(cand.read_text(encoding="utf-8")).get("spots", []))
                  if cand.exists() and has_park else 0)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    by_city = sorted(cities.items(), key=lambda x: -x[1])
    # 出どころの説明は、実際の作り方に合わせて書き分ける
    parts = []
    if rows:
        parts.append(f"隣の市区町村が自分のホームページで載せている行き先を 1 件ずつ当たったもの {len(rows)} 件")
    if len(spots) - len(rows):
        parts.append("区市町村をまたいで配られているデータセットから取ったもの "
                     f"{len(spots) - len(rows)} 件")
    note = "／".join(parts)
    (OUT_DIR / f"{stage.id}_outside.json").write_text(json.dumps({
        "stage": stage.id, "spots": spots, "by_city": [list(x) for x in by_city],
        "cities": len(cities), "osm_candidates": candidates,
        "hand": len(rows), "from_dataset": len(spots) - len(rows),
        "source_note": note,
    }, ensure_ascii=False), encoding="utf-8")
    print(f"  {stage.name}: 市外の行き先 {len(spots)} 件／"
          + "・".join(f"{c} {n}" for c, n in by_city))
    if candidates and rows:
        print(f"    （OpenStreetMap の候補 {candidates} 件のうち、"
              f"公式ページを当たって {len(rows)} 件を選んだ）")
    if missing:
        print("  ! 住所から座標が引けなかった: " + "／".join(missing))


if __name__ == "__main__":
    all_stages = stages_mod.load_all()
    targets = [all_stages[sys.argv[1]]] if len(sys.argv) > 1 else list(all_stages.values())
    for stage in targets:
        build(stage)
