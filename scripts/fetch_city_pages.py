#!/usr/bin/env python3.9
"""自治体ホームページを巡回して、スポットの概要のもとになる事実を集める。

    python3.9 scripts/fetch_city_pages.py --stage nagareyama --section institution
    python3.9 scripts/fetch_city_pages.py --stage nagareyama --section child

オープンデータ（CSV）には施設の概要も公式ページの URL も入っていない。
市ホームページの施設案内には 1 施設 1 ページがあり、遊具・トイレ・開館時間・
最寄り駅からの所要が書かれている。ここから**事実だけ**を構造化して取り出す。

**どこを巡回するかは舞台ごとに違う**（流山市は「施設案内」と「子育て」）ので、
巡回先は `data/stages/<stage>.yaml` の `web_pages` に置いてある。
コードには自治体名も URL も書かない。

市ホームページのコンテンツは著作権の対象で無断転載はできない（リンクは自由）。
そのため取り出すのは「遊具の種類と数」「トイレの有無」のような事実に限り、
本文の文章はそのまま持たず、概要文は build_spot_details.py で自前に組み立てる。
"""
import argparse
import concurrent.futures
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = ""            # 舞台の設定から入れる（parse() が外部リンクの判定に使う）
UA = "kosodate-navi/0.1 (ODC2026 prototype; contact via GitHub)"
WORKERS = 3   # 市のサーバに負荷をかけない範囲で並行に取る

SKIP_PAT = re.compile(r"/(cgi-bin|cgi-mgr|cgi-evt|cgi-opd)/|\.(pdf|xlsx?|csv|zip|jpg|png|gif)$", re.I)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return raw.decode("utf-8", "replace")


def safe_fetch(url, tries=3):
    """たまに応答が返らないページがある。**索引ページを 1 回落とすとその下が丸ごと
    取れなくなる**ので、必ず数回やり直す。"""
    for attempt in range(tries):
        try:
            return fetch(url)
        except Exception as e:                        # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    print("  ! %s %s" % (url, last), file=sys.stderr)
    return None


def main_html(doc):
    """本文（h1 から アンケートフォームの手前まで）を切り出す。"""
    i = doc.find("<h1")
    if i < 0:
        return ""
    j = doc.find('<div id="pagequest"', i)
    if j < 0:
        j = doc.find('<div id="reference"', i)
    # 終わりの目印が無いページがある。**一覧ページは長い**ので（区立公園の一覧は
    # 1 つの表に 100 件以上）、ここで切ると表の後ろ半分が落ちる
    return doc[i: j if j > 0 else i + 200000]


def text_of(fragment):
    t = re.sub(r"(?s)<(script|style).*?</\1>", "", fragment)
    t = re.sub(r"<br[^>]*>", "\n", t)
    t = re.sub(r"</(p|div|li|dd|dt|tr|h[1-6])>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = t.replace("　", " ")
    return re.sub(r"[ \t]+", " ", t).strip()


def parse(url, doc):
    body = main_html(doc)
    h1 = re.search(r"(?s)<h1[^>]*>(.*?)</h1>", body)
    title = text_of(h1.group(1)) if h1 else ""
    title = re.sub(r"^施設案内\s*", "", title).strip()

    # 定義リスト（所在地・開館時間・遊具施設 …）
    fields = {}
    for dl in re.findall(r"(?s)<dl[^>]*>(.*?)</dl>", body):
        for dt, dd in re.findall(r"(?s)<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", dl):
            k, v = text_of(dt), text_of(dd)
            if k:
                fields.setdefault(k, v)
    # 表（開館時間などが table のページ）
    for tr in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", body):
        cells = re.findall(r"(?s)<t[hd][^>]*>(.*?)</t[hd]>", tr)
        if len(cells) == 2:
            k, v = text_of(cells[0]), text_of(cells[1])
            if k and v and len(k) < 20:
                fields.setdefault(k, v)

    # **バリアフリー対応状況のアイコン**。授乳室・貸出用ベビーカー・多目的トイレが
    # ここにしか無い（オープンデータの CSV には 1 件も入っていない）。
    icons = []
    icon_box = re.search(r'(?s)<div id="shisetsuicon">(.*?)</div>', body)
    if icon_box:
        for alt in re.findall(r'<img[^>]*alt="([^"]*)"', icon_box.group(1)):
            alt = alt.replace("があります", "").strip()
            if alt and "アイコンの説明" not in alt:
                icons.append(alt)

    # 写真の説明文と、地図の下のアクセス案内
    caption = re.search(r"(?s)<figcaption[^>]*>(.*?)</figcaption>", body)
    access = ""
    m = re.search(r"(?s)<h2[^>]*>\s*地図\s*</h2>(.*)", body)
    if m:
        access = text_of(m.group(1))

    # 表（赤ちゃんほっとスペースの設備一覧など、CSV に無い事実が入っている）
    tables = []
    for tbl in re.findall(r"(?s)<table[^>]*>(.*?)</table>", body):
        rows = []
        for tr in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", tbl):
            cells = [text_of(c) for c in re.findall(r"(?s)<t[hd][^>]*>(.*?)</t[hd]>", tr)]
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append(rows)

    coord = re.search(r"maps\?q=([\d.]+),([\d.]+)", body)
    update = re.search(r"更新日\s*([^<\n]+)", text_of(body))
    section = re.search(r'(?s)<div id="reference".*?</div>', doc)
    ext = []
    for u, label in re.findall(r'href="(https?://[^"]+)"[^>]*>(.*?)</a>', body):
        if ROOT in u or "twitter.com" in u or "facebook" in u or "line.me" in u or "maps.google" in u:
            continue
        ext.append({"url": u, "label": text_of(label)})

    return {
        "url": url,
        "page_id": (re.search(r"ページ番号(\d+)", text_of(body)) or [None, None])[1]
        if re.search(r"ページ番号(\d+)", text_of(body)) else None,
        "title": title,
        "fields": fields,
        "lat": float(coord.group(1)) if coord else None,
        "lon": float(coord.group(2)) if coord else None,
        "updated": update.group(1).strip() if update else None,
        "external_links": ext,
        "barrierfree": icons,
        "caption": text_of(caption.group(1)) if caption else None,
        "access": access,
        "tables": tables,
        "text": text_of(body),
    }


def crawl(out_dir, start_paths, prefixes, max_pages=2000):
    """幅優先で巡回する。1 ページ 1 秒強かかるので、ウェーブごとに数本だけ並行に取る。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    html_dir = out_dir / "html"
    html_dir.mkdir(exist_ok=True)
    seen, queue, pages = set(), list(start_paths), []

    def want(url):
        return (any(url.startswith(ROOT + p) for p in prefixes) and url not in seen
                and not SKIP_PAT.search(url))

    while queue and len(seen) < max_pages:
        wave, queue = queue, []
        targets = []
        for path in wave:
            url = urllib.parse.urljoin(ROOT, path).split("#")[0]
            if want(url):
                seen.add(url)
                targets.append(url)
        if not targets:
            break
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for url, doc in zip(targets, pool.map(safe_fetch, targets)):
                if doc is None:
                    continue
                rec = parse(url, doc)
                # 本文 HTML を残す（表の読み方を変えたくなっても取り直さなくてよい）
                name = re.sub(r"[^0-9a-zA-Z]+", "_", url[len(ROOT):]).strip("_")
                (html_dir / (name + ".html")).write_text(main_html(doc), encoding="utf-8")
                pages.append(rec)
                for href in re.findall(r'href="([^"]+)"', main_html(doc)):
                    nxt = urllib.parse.urljoin(url, href).split("#")[0]
                    if want(nxt):
                        queue.append(nxt)
        print("  %d ページ取得／残り %d" % (len(pages), len(set(queue))), flush=True)
    return pages


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="nagareyama")
    ap.add_argument("--section", default=None, help="舞台の web_pages.sections のどれか")
    ap.add_argument("--out", default=None)
    ap.add_argument("--max", type=int, default=2000)
    ap.add_argument("--reparse", action="store_true",
                    help="保存済みの HTML から読み直す（取り直さない）")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo))
    from backend import stages as stages_mod          # noqa: E402

    stage = stages_mod.load_all()[args.stage]
    conf = stage.web_pages
    if not conf:
        sys.exit(f"{stage.name} には web_pages の設定がありません（data/stages/{stage.id}.yaml）")
    ROOT = conf["root"].rstrip("/")
    sections = conf["sections"]
    out = Path(args.out) if args.out else stage.raw_dir / "pages"
    if not out.is_absolute():
        out = repo / out

    if args.reparse:
        # 取り出す事実を増やしたときに、市のサーバに取りに行かずに読み直す
        buckets = {name: [] for name in sections}
        for path in sorted((out / "html").glob("*.html")):
            url = ROOT + "/" + path.stem.replace("_", "/").replace("/html", ".html")
            rec = parse(url, path.read_text(encoding="utf-8"))
            for name, sec in sections.items():
                if any(url.startswith(ROOT + p) for p in sec["prefixes"]):
                    buckets[name].append(rec)
                    break
        for name, recs in buckets.items():
            if not recs:
                continue
            with open(out / ("%s.jsonl" % name), "w", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            print("%s: %d ページを読み直しました" % (name, len(recs)))
        sys.exit(0)

    todo = [args.section] if args.section else list(sections)
    for name in todo:
        sec = sections[name]
        pages = crawl(out, sec["start"], sec["prefixes"], args.max)
        dest = out / ("%s.jsonl" % name)
        with open(dest, "w", encoding="utf-8") as f:
            for p in pages:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
        print("%d ページ → %s" % (len(pages), dest))
