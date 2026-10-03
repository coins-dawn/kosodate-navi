#!/usr/bin/env python3.9
"""市ホームページの施設ページから、スポットの**概要と公式ページ**を独自整備する。

    python3.9 scripts/build_spots.py nagareyama        # まずスポットを作り
    python3.9 scripts/build_spot_details.py nagareyama # 概要と公式ページを付け
    python3.9 scripts/build_spots.py nagareyama        # もう一度流して取り込む

出力: data/spots/<stage>_details.csv（git 管理下。これが独自整備データの本体）

**なぜ要るか**: オープンデータの CSV には座標と電話しか無い。行き先を選ぶときに
知りたいのは「そこに何があるか」と「公式ページ」で、どちらも CSV には 1 件も無い。
市の施設案内ページには遊具・利用時間・バリアフリー設備（授乳室・貸出用ベビーカー・
多目的トイレ）が載っているので、そこから**事実だけ**を取り出して自前の表にする。

**著作権**: 流山市ホームページの文章は無断転載できない（リンクは自由）。
そのため本文の文章はコピーせず、取り出した事実（遊具の種類・面積・利用時間・
設備アイコン）から `summary` を**自前の言い回しで組み立てる**。
"""
import csv
import json
import math
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import stages as stages_mod  # noqa: E402

REPO = stages_mod.REPO_ROOT
TODAY = "2026-09-23"
# 自前のページを持たない施設（店や銀行の中の授乳スペース）は、市の紹介ページに寄せる
HOTSPACE_PAGE = "https://www.city.nagareyama.chiba.jp/institution/1042534.html"

# 遊具の表記ゆれをそろえる（市のページは「滑り台」「すべり台」が混在）
TOY_ALIASES = {"滑り台": "すべり台", "小型ブランコ": "ブランコ", "大型ブランコ": "ブランコ",
               "複合系遊具": "複合遊具", "大型複合遊具": "複合遊具",
               "ブランコ付き複合遊具": "複合遊具", "スプリンク遊具": "スプリング遊具",
               "置物遊具": "置物", "肋木": "助木"}
TOY_DROP = {"外", "その他遊具", "2基", "置物", "コンクリート遊具", "土管遊具"}

# 子連れに効くバリアフリーアイコンだけを拾う（肯定形で出せるものだけ）
ICON_LABELS = [("授乳室", "授乳室"), ("貸出用ベビーカー", "貸出用ベビーカー"),
               ("多目的トイレ", "多目的トイレ"), ("車いす用トイレ", "車いす用トイレ"),
               ("車いす対応の出入口", "段差のない出入口"),
               ("車いす対応エレベータ", "エレベーター"), ("一般エレベータ", "エレベーター"),
               ("一般駐車場", "駐車場"), ("AED", "AED")]

# 同じ施設が AED 一覧や災害用井戸一覧にも載っている。**行き先として見たい側**の
# ページを選ぶ（AED 一覧のページには開館時間も遊具も書かれていない）。
# 施設の説明が書かれていないページ（AED の設置場所一覧など）は、突合の相手にしない
SKIP_CATEGORIES = {"AED設置施設", "災害用井戸設置施設", "市民トイレ協力店", "駐車場・駐輪場"}

CATEGORY_RANK = ["子育て施設", "公園", "文化施設", "コミュニティ施設", "保健施設",
                 "青少年施設", "教育施設", "市役所・出張所", "福祉施設", "観光施設",
                 "生活関連施設", "スポーツ施設", "その他", "AED設置施設",
                 "災害用井戸設置施設"]

# 市のページの置き場所から分かる施設の種類。**オープンデータでは
# 「赤ちゃんほっとスペース」としか分からない図書館**に、正しい種類を付けられる。
CATEGORY_TYPES = {
    "図書館": "図書館", "公民館": "公民館", "児童館・児童センター": "児童館・児童センター",
    "地域子育て支援センター": "子育て支援センター", "幼稚園": "幼稚園",
    "学童クラブ": "学童クラブ", "公園（北部地域）": "公園", "公園（東部地域）": "公園",
    "公園（中部地域）": "公園", "公園（南部地域）": "公園",
}

# 種類そのものの説明。**一覧ページにしか書かれていない**ので、施設ページからは取れない。
# 事実を確かめた上で自前の言い回しで書く（出典は type_source）。
TYPE_LEADS = {
    "児童館・児童センター": (
        "午前9時から12時は乳幼児と保護者の時間、午後1時から5時は小・中・高校生の時間です。"
        "0歳から18歳まで、無料で使えます",
        "https://www.city.nagareyama.chiba.jp/life/1001107/1001229/1001231.html", True),
    "子育て支援センター": (
        "保育園の園庭やホールを開放している、就学前の子どもと保護者のための場所です。"
        "育児の相談もできます",
        "https://www.city.nagareyama.chiba.jp/life/1001107/1001239.html", False),
}

STATION_RE = re.compile(
    r"([ぁ-んァ-ヶ一-龥A-Za-z・ー]{2,20}駅)[^。\n]{0,8}?(?:から|より|下車)[^。\n]{0,12}?徒歩(?:約)?(\d+)分")
# 「つくばエクスプレス流山おおたかの森駅」から路線名を落として駅名だけにする
LINE_NAMES = ["つくばエクスプレス線", "つくばエクスプレス", "東武アーバンパークライン",
              "東武野田線", "東武鉄道", "流鉄流山線", "流山線", "流鉄", "JR常磐線",
              "JR武蔵野線", "常磐線", "武蔵野線", "東武線", "野田線"]


# 全角と半角、「ケ」と「ヶ」が出どころによってばらばら（CSV は「南流山１号公園」、
# 市のページは「南流山1号公園」）。突合の前にそろえる。
ZEN = "".join(chr(0xFF01 + i) for i in range(94))
HAN = "".join(chr(0x21 + i) for i in range(94))
TO_HAN = str.maketrans(ZEN, HAN)


def norm(name):
    n = (name or "").replace("　", "").replace(" ", "").translate(TO_HAN)
    n = re.sub(r"[（(].*?[）)]", "", n)
    n = n.replace("ケ", "ヶ").replace("ツ", "ッ").replace("・", "")
    return n.replace("流山市立", "").replace("流山市", "").strip()


def dist_m(a_lat, a_lon, b_lat, b_lon):
    return math.hypot((a_lat - b_lat) * 111000, (a_lon - b_lon) * 91000)


def load_pages(stage):
    out = []
    for name in ("institution", "child"):
        path = stage.raw_dir / "pages" / f"{name}.jsonl"
        if not path.exists():
            continue
        for line in open(path, encoding="utf-8"):
            rec = json.loads(line)
            rec["section"] = name
            out.append(rec)
    return out


def category_map(pages):
    """URL の親をたどって「文化施設 > 図書館」を作る。**市のページの置き場所が
    施設の種類を教えてくれる**（オープンデータの CSV には種別が無い施設がある）。"""
    index = {p["url"]: p["title"] for p in pages if p["url"].endswith("index.html")}

    def label_of(url):
        parts = url.split("/")
        names = []
        for i in range(4, len(parts)):
            parent = "/".join(parts[:i]) + "/index.html"
            if index.get(parent):
                names.append(index[parent])
        return " > ".join(names)

    return {p["url"]: label_of(p["url"]) for p in pages}


def toys_of(page):
    raw = page["fields"].get("遊具施設") or ""
    toys, seen = [], set()
    for part in re.split(r"[、,　 ]+", raw.replace("\n", "")):
        part = re.sub(r"[（(][^）)]*[）)]", "", part).strip()
        part = TOY_ALIASES.get(part, part)
        if not part or part in TOY_DROP or part in seen:
            continue
        seen.add(part)
        toys.append(part)
    return toys


def area_of(page):
    m = re.search(r"面積[：:]\s*約?([\d.]+)\s*(ヘクタール|平方メートル|㎡)", page["text"])
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2)
    if unit == "ヘクタール":
        return f"約{value:g}ヘクタール"
    return f"約{value:,.0f}平方メートル"


def hours_of(page):
    for key in ("開館時間", "利用時間", "利用可能な時間", "営業時間", "開園時間"):
        v = page["fields"].get(key)
        if v:
            return re.sub(r"\s*\n+\s*", " ", v).strip()
    return None


def kids_program_of(page):
    """図書館のおはなし会など、子ども向けの催しがあるか。**日時の事実だけ**を見る。"""
    keys = [k for k in page["fields"] if "おはなし会" in k or "子ども" in k or "親子" in k]
    if not keys:
        return None
    blob = " ".join(page["fields"][k] for k in keys)
    if "赤ちゃん" in blob:
        return "おはなし会と、赤ちゃん向けのおはなし会があります"
    return "子ども向けのおはなし会があります"


def icons_of(page):
    got = []
    for key, label in ICON_LABELS:
        if any(key in i for i in page.get("barrierfree") or []) and label not in got:
            got.append(label)
    return got


def station_of(page):
    blob = " ".join([page.get("access") or "", page["fields"].get("交通アクセス") or ""])
    m = STATION_RE.search(blob.replace("\n", " "))
    if not m:
        return (None, None)
    station = m.group(1)
    for line in LINE_NAMES:
        if line in station:
            station = station.split(line)[-1]
    station = re.sub(r"^[線\s・]+", "", station)
    return (station if len(station) >= 3 else None, int(m.group(2)))


def official_link(page):
    """施設が自分のホームページを持っていれば、そちらを公式にする。"""
    for link in page.get("external_links") or []:
        label = link.get("label") or ""
        if any(x in label for x in ("アドビ", "駐車場位置", "地図", "Adobe")):
            continue
        if any(x in link["url"] for x in ("goo.gl/maps", "google.com/maps", "maps.app.goo")):
            continue      # 地図へのリンクは公式ページではない
        url = link["url"]
        if "safe-redirect/" in url:
            # 市のページはリンクをウイルス検査サービス経由にしている。本来の URL に戻す
            url = urllib.parse.unquote(url.split("safe-redirect/", 1)[1])
        if "ホームページ" in label or "外部リンク" in label:
            label = re.sub(r"\s*（外部リンク）\s*", "", label).strip()
            if len(label) <= 4 or "ホームページ" in label:
                label = "公式ホームページ"
            return url, label
    return None, None


def phrase_hours(hours):
    """「午前9時～午後5時」はそのまま、「フロア開放 月～金 …」は前置きを変える。"""
    if not hours:
        return None
    hours = re.split(r"[※（(]ただし|※", hours)[0].strip(" 　。")
    if len(hours) > 34:      # 長い注記は欄で見せる。概要は一目で読める長さに保つ
        hours = re.split(r"[（(]", hours)[0].strip(" 　")
    if "曜" in hours or len(hours) > 26:
        return f"開いているのは{hours}"
    return f"{hours}に開いています"


def summarize(spot, page, facts):
    """**事実から自前で組み立てる。**市のページの文章はコピーしない。"""
    ftype = facts.get("type_hint") or spot.get("facility_type") or ""
    bits = []
    if ftype == "公園":
        toys = facts["toys"]
        if toys:
            bits.append("・".join(toys[:4]) + "のある公園です")
        elif facts["toy_count"]:
            bits.append(f"遊具が{facts['toy_count']}基ある公園です")
        else:
            bits.append("市が管理する公園です")
        if facts["area"]:
            bits.append(f"広さは{facts['area']}")
        if facts["toilet"]:
            bits.append("トイレがあります")
    else:
        label = ftype or "施設"
        lead = TYPE_LEADS.get(ftype)
        hours = phrase_hours(facts["hours"])
        if lead:
            bits.append(lead[0])
            if not lead[2] and hours:
                bits.append(hours)
        else:
            bits.append(f"{hours}（{label}）" if hours else f"{label}です")
        # 休館日は長い文章になりがち。短いときだけ概要に入れて、長いものは欄で見せる
        if facts["closed"] and len(facts["closed"]) <= 24:
            bits.append(f"休みは{facts['closed']}")
    kid = [i for i in facts["icons"] if i in ("授乳室", "貸出用ベビーカー", "多目的トイレ")]
    if facts["equipment"]:      # ほっとスペースの表と同じことを二度言わない
        kid = [i for i in kid if i not in ("授乳室", "多目的トイレ")]
    if kid:
        bits.append("・".join(kid) + "があります")
    if facts["kids_program"]:
        bits.append(facts["kids_program"])
    if facts["equipment"]:
        bits.append(facts["equipment"])
    if facts["station"] and facts["walk_min"]:
        bits.append(f"{facts['station']}から徒歩{facts['walk_min']}分")
    return "。".join(b.rstrip("。") for b in bits) + "。"


def hotspace_equipment(pages):
    """赤ちゃんほっとスペースの設備は**表にしか無い**（CSV は座標と電話だけ）。"""
    out = {}
    for page in pages:
        if not page["url"].endswith("1042534.html"):
            continue
        for table in page.get("tables") or []:
            for row in table:
                if len(row) < 6 or row[0] in ("施設名", ""):
                    continue
                # 1 行目が施設名、2 行目からは**建物の中のどこか**（「2F 赤ちゃん休憩室」）。
                # 授乳室が建物のどこにあるかは、ここにしか書かれていない。
                lines = [x.strip() for x in row[0].split("\n") if x.strip()]
                if not lines:
                    continue
                out[norm(lines[0])] = {
                    "diaper_table": row[3].strip() == "〇",
                    "nursing_room": row[4].strip() == "〇",
                    "hot_water": row[5].strip() == "〇",
                    "tel": row[2].strip(),
                    "where": "／".join(lines[1:]),
                }
    return out


def support_center_sites(pages):
    """地域子育て支援拠点の**公式ホームページ**は、くらし案内のページに並んでいる。"""
    out = {}
    for page in pages:
        if not page["url"].endswith("1001239.html"):
            continue
        for link in page.get("external_links") or []:
            label = link.get("label") or ""
            if "アドビ" in label or "ホームページ" not in label:
                continue
            name = re.sub(r"ホームページ.*$", "", label)
            name = re.sub(r"[（(][^）)]*[）)]", "", name).strip()
            name = name.replace("地域子育て支援拠点", "").replace("支援センター", "").strip()
            if name:
                out[norm(name)] = (link["url"], "公式ホームページ")
    return out


def load_overrides(stage):
    """**人が確かめて直した分**。廃止された施設や、市のページに無い公式サイトを入れる。
    生成し直しても消えないように、別ファイルにしている。"""
    path = REPO / "data" / "spots" / f"{stage.id}_overrides.csv"
    if not path.exists():
        return {}
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("name"):
                out[norm(row["name"])] = {k: (v or "").strip() for k, v in row.items()}
    return out


def build(stage):
    pages = load_pages(stage)
    spots = json.loads((REPO / "data" / "spots" / "generated" / f"{stage.id}.json")
                       .read_text(encoding="utf-8"))["spots"]
    equipment = hotspace_equipment(pages)
    categories = category_map(pages)
    centers = support_center_sites(pages)

    def rank(page):
        top = (categories.get(page["url"]) or "").split(" > ")[0]
        return CATEGORY_RANK.index(top) if top in CATEGORY_RANK else len(CATEGORY_RANK)

    by_name = {}
    for page in pages:
        if not page.get("title"):
            continue
        if set((categories.get(page["url"]) or "").split(" > ")) & SKIP_CATEGORIES:
            continue
        for key in {norm(page["title"])} | {norm(m) for m in
                                            re.findall(r"[（(]([^）)]+)[）)]", page["title"])}:
            by_name.setdefault(key, []).append(page)
    for key in by_name:
        by_name[key].sort(key=rank)

    overrides = load_overrides(stage)
    rows, matched = [], 0
    for spot in spots:
        page = None
        keys = [norm(spot["name"])]
        for inner in re.findall(r"[（(]([^）)]+)[）)]", spot["name"]):
            keys += [norm(inner)] + [norm(x) for x in inner.split("・") if len(x) > 4]
        cands = [c for k in keys for c in by_name.get(k, [])]
        if not cands:
            # 「サンコーテクノプラザ（南流山児童センター…）」のように、
            # 市のページ名が施設名を含む形。**近くにあるときだけ**同じ施設とみなす。
            cands = [p for p in pages if p["lat"] and p.get("title")
                     and not set((categories.get(p["url"]) or "").split(" > ")) & SKIP_CATEGORIES
                     and any(k and k in norm(p["title"]) for k in keys)
                     and dist_m(spot["lat"], spot["lon"], p["lat"], p["lon"]) < 300]
        cands.sort(key=rank)
        for cand in cands:
            if cand["lat"] and dist_m(spot["lat"], spot["lon"], cand["lat"], cand["lon"]) > 1500:
                continue      # 同名の別施設（隣の市の公園など）を避ける
            page = cand
            break
        # 座標だけで結ぶのは駄目だった（同じ座標に駐車場やイベント会場のページがある）。
        # **名前が一致したものだけ**を同じ施設とみなす。
        equip = equipment.get(norm(spot["name"])) or {}
        fix = overrides.get(norm(spot["name"])) or {}
        official = centers.get(norm(spot["name"]))
        if page is None and not equip and not official and not fix:
            continue
        matched += 1

        facts = {"toys": [], "toy_count": None, "area": None, "toilet": False,
                 "hours": None, "closed": None, "icons": [], "station": None,
                 "walk_min": None, "equipment": None, "kids_program": None,
                 "type_hint": None}
        city_url = tel = page_updated = None
        if page is not None:
            facts["toys"] = toys_of(page)
            count = re.search(r"遊具(\d+)基", page["fields"].get("所在地") or "")
            facts["toy_count"] = int(count.group(1)) if count else None
            facts["area"] = area_of(page)
            facts["toilet"] = "トイレ有" in (page["fields"].get("所在地") or "")
            facts["hours"] = hours_of(page)
            facts["closed"] = (page["fields"].get("休館日") or
                               page["fields"].get("定休日") or "").replace("\n", " ").strip() or None
            facts["icons"] = icons_of(page)
            facts["station"], facts["walk_min"] = station_of(page)
            facts["kids_program"] = kids_program_of(page)
            leaf = (categories.get(page["url"]) or "").split(" > ")[-1]
            facts["type_hint"] = CATEGORY_TYPES.get(leaf)
            city_url = page["url"]
            tel = (page["fields"].get("電話番号") or "").split("\n")[0].strip() or None
            page_updated = page.get("updated")
            if official is None:
                url, label = official_link(page)
                official = (url, label) if url else None
        tel = tel or equip.get("tel")
        if equip:
            can = [k for k, v in (("授乳", equip.get("nursing_room")),
                                  ("おむつ替え", equip.get("diaper_table"))) if v]
            text = "・".join(can) + "ができます" if can else ""
            if equip.get("hot_water"):
                text = (text + "（調乳用のお湯もあります）") if text else "調乳用のお湯があります"
            if equip.get("where"):
                spots_ = equip["where"].split("／")
                where = spots_[0] + ("ほか%dか所" % (len(spots_) - 1) if len(spots_) > 1 else "")
                text = (text + "。場所は" + where) if text else "場所は" + where
            facts["equipment"] = text or None

        summary = summarize(spot, page, facts) if page is not None else ""
        if equip and not summary:
            summary = ("外出のとちゅうに立ち寄れる、市が認定した赤ちゃんほっとスペースです。"
                       + (facts["equipment"] + "。" if facts["equipment"] else ""))
        rows.append({
            "name": spot["name"],
            "lat": round(spot["lat"], 6), "lon": round(spot["lon"], 6),
            "facility_type": spot.get("facility_type") or "",
            "summary": summary,
            "official_url": (official or (None, None))[0] or city_url or (
                HOTSPACE_PAGE if equip else ""),
            "official_label": ((official or (None, None))[1]
                               or ("流山市 施設案内" if city_url
                                   else ("流山市 赤ちゃんほっとスペース" if equip else ""))),
            "city_page_url": city_url or "",
            "city_category": categories.get(city_url, "") if city_url else "",
            "facility_type_hint": facts["type_hint"] or "",
            "tel": tel or "",
            "where": equip.get("where") or "",
            "open_hours": facts["hours"] or "",
            "closed_days": facts["closed"] or "",
            "playground": "・".join(facts["toys"]),
            "toilet": "1" if facts["toilet"] else "",
            "area": facts["area"] or "",
            "nursing_room": "1" if (equip.get("nursing_room") or
                                    any("授乳室" in i for i in facts["icons"])) else "",
            "diaper_table": "1" if (equip.get("diaper_table") or
                                    any("多目的トイレ" in i for i in facts["icons"])) else "",
            "hot_water": "1" if equip.get("hot_water") else "",
            "stroller_lend": "1" if any("ベビーカー" in i for i in facts["icons"]) else "",
            "barrier_free": "・".join(i for i in facts["icons"] if "ができます" not in i),
            "station": facts["station"] or "",
            "walk_min": facts["walk_min"] or "",
            "type_source": (TYPE_LEADS.get(facts["type_hint"]
                                           or spot.get("facility_type")) or ("", "", ""))[1],
            "page_updated": page_updated or "",
            "status": "",
            "status_note": "",
            "checked_at": TODAY,
        })
        if fix:
            for key, value in fix.items():
                if key in rows[-1] and value:
                    rows[-1][key] = value

    # 廃止された施設は、いちど地図から消すと generated からも消える。
    # **手当ての行は残し続ける**（消えたままだと、次に作り直したとき復活してしまう）
    seen = {norm(r["name"]) for r in rows}
    for key, fix in overrides.items():
        if key in seen:
            continue
        row = {field: "" for field in rows[0]}
        row.update({k: v for k, v in fix.items() if k in row})
        row["checked_at"] = fix.get("checked_at") or TODAY
        rows.append(row)

    dest = REPO / "data" / "spots" / f"{stage.id}_details.csv"
    with open(dest, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in sorted(rows, key=lambda r: (r["facility_type"], r["name"])):
            writer.writerow(row)
    print(f"  {matched} 件に概要と公式ページを付けました → {dest}")
    print(f"    うち 概要あり {sum(1 for r in rows if r['summary'])} 件 / "
          f"公式ページあり {sum(1 for r in rows if r['official_url'])} 件 / "
          f"授乳室 {sum(1 for r in rows if r['nursing_room'])} 件")


if __name__ == "__main__":
    all_stages = stages_mod.load_all()
    target = sys.argv[1] if len(sys.argv) > 1 else "nagareyama"
    build(all_stages[target])
