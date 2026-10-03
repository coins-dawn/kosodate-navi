#!/usr/bin/env python3.9
"""**GitHub Pages に置ける静的サイト**を作る。API の結果を先に計算してファイルにする。

    python3.9 scripts/build_static.py                 # 全部作る
    python3.9 scripts/build_static.py --only site     # 画面のファイルだけ置き直す
    python3.9 scripts/build_static.py --stage nagareyama --radii 1000 --max-routes 5

出力: site/（そのまま GitHub Pages で公開できる）

**なぜ事前計算するのか**: サーバ版は 1 リクエストで最大 1.5GB のメモリを使う
（大都市の `stop_time` は 100 万行を超え、それをメモリに載せて RAPTOR を回すため）。無料の PaaS は
512MB 級なので載らない。**出発時刻を固定すれば答えは有限**なので、先に全部計算して
JSON として配れば、サーバもメモリも要らなくなる。

- 出発時刻は `data/snapshots.yaml` の「スナップショット」。増やすと生成物も増える
- 中身は**サーバ版の API をそのまま呼んで保存する**（Flask の test_client）。
  そのため静的版とサーバ版でレスポンスの形が必ず一致する
- 天気だけは焼き込まない（`KOSODATE_NO_WEATHER=1`）。数日後に「今日の天気」が
  嘘になるため、静的版はブラウザから気象庁を直接取る

出来上がりの形（画面の `API` がこの形で取りに行く）:

    site/index.html, app.js, style.css, config.js
    site/api/snapshots.json
    site/api/stages.json
    site/api/<stage>/boundary.json, stations.json, coverage.json, network.json
    site/api/<stage>/spot/<spot_id>.json
    site/api/<stage>/<snapshot>/events.json
    site/api/<stage>/<snapshot>/nearby/<home>/<radius_m>.json
    site/api/<stage>/<snapshot>/routes/<home>/<profile>/<spot_id>.json
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

os.environ.setdefault("KOSODATE_NO_WEATHER", "1")   # import より先に置く

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import app as app_mod          # noqa: E402
from backend import stages as stages_mod    # noqa: E402

REPO = stages_mod.REPO_ROOT
SITE = REPO / "site"
# 画面のスライダと同じ刻み（frontend/index.html の #radius・app.js の RADIUS_*）。
# **3 つを必ずそろえる。** 静的版はここで作った半径のファイルしか持っていない。
RADII = list(range(200, 6001, 200))         # 200m〜6km を 200m 刻み
PROFILES = ["walk", "stroller"]             # 経路のプロファイル（ベビーカーの有無）
# **自転車と車は事前計算しない**（2026-09-30・ユーザー指示）。
# 公共交通ではなく、推奨されているオープンデータも使っていないため
ROUTE_MODES = ["total", "train", "bus", "walk"]


def safe_id(spot_id):
    """スポット ID をファイル名にする（`nagareyama_local:0` → `nagareyama_local_0`）。"""
    return re.sub(r"[^A-Za-z0-9_-]", "_", spot_id)


def write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path.stat().st_size


class Builder:
    def __init__(self, client, out: Path):
        self.client = client
        self.out = out
        self.bytes = 0
        self.files = 0

    def get(self, url):
        res = self.client.get(url)
        if res.status_code != 200:
            raise RuntimeError(f"{url} → {res.status_code}")
        return res.get_json()

    def post(self, url, body):
        res = self.client.post(url, json=body)
        if res.status_code != 200:
            raise RuntimeError(f"{url} → {res.status_code}")
        return res.get_json()

    def save(self, rel, data):
        self.bytes += write(self.out / rel, data)
        self.files += 1


def build_api(bld, stages, snapshots, only_stage=None, radii=None, max_routes=None,
              skip_routes=False):
    radii = radii or RADII
    bld.save("api/snapshots.json", snapshots)
    bld.save("api/stages.json", bld.get("/api/stages"))
    bld.save("api/site.json", bld.get("/api/site"))     # 問い合わせ先（ガイドライン 3.1）
    bld.save("api/profiles.json", bld.get("/api/profiles"))

    for sid, stage in sorted(stages.items()):
        if only_stage and sid != only_stage:
            continue
        print(f"== {stage.name} ==", flush=True)
        bld.save(f"api/{sid}/boundary.json", bld.get(f"/api/boundary?stage={sid}"))
        bld.save(f"api/{sid}/stations.json", bld.get(f"/api/stations?stage={sid}"))
        bld.save(f"api/{sid}/coverage.json", bld.get(f"/api/coverage?stage={sid}"))
        # **経路探索が使っている路線と乗り場**（地図の下敷き）。舞台に 1 つでよい
        bld.save(f"api/{sid}/network.json", bld.get(f"/api/network?stage={sid}"))

        # スポットの吹き出し。**さがす範囲に入らないものも含めて全部**作っておく
        # （1 件 1〜3KB と小さいので、取りこぼしを心配するより安い）。
        # `/api/spots` は年齢で絞ってしまうので、索引から直に id を取る
        index = app_mod.ctx(sid).spots
        ids = [s["id"] for s in index.spots + index.outside]
        for spot_id in ids:
            bld.save(f"api/{sid}/spot/{safe_id(spot_id)}.json",
                     bld.get(f"/api/spot/{spot_id}?stage={sid}"))
        print(f"  スポットの吹き出し {len(ids)} 件", flush=True)

        for snap in snapshots:
            when = f"date={snap['date']}&time={snap['time']}"
            head = f"api/{sid}/{snap['id']}"
            bld.save(f"{head}/events.json",
                     bld.get(f"/api/events?stage={sid}&date={snap['date']}"))
            for home in stage.homes:
                hid = home["id"]
                started = time.time()
                # 行き先。**半径ごとに 1 ファイル**（画面はスライダを離したときに取りに来る）
                reachable = {}
                for r in radii:
                    d = bld.get(f"/api/nearby?stage={sid}&home={hid}&radius_m={r}&{when}")
                    bld.save(f"{head}/nearby/{hid}/{r}.json", d)
                    home_point = d["home"]
                    # いちばん大きい半径に、ほかの半径の行き先がすべて入る（円は入れ子）
                    for s in d["spots"]:
                        reachable[s["id"]] = s
                print(f"  {snap['id']} / {home['name']}: さがす範囲 {len(radii)} 通り "
                      f"（{time.time() - started:.0f}秒）／"
                      f"行き先 {len(reachable)} 件", flush=True)

                # 経路。**いちばん広い半径に入る行き先だけ**作る（画面で選べるのはそれだけ）。
                # 経路は作り直しに時間がかかるので、行き先だけ直したいときは --skip-routes
                if skip_routes:
                    print("  （経路は作り直さない）", flush=True)
                    continue
                targets = list(reachable.values())[:max_routes] if max_routes \
                    else list(reachable.values())
                started = time.time()
                for i, s in enumerate(targets, 1):
                    for prof in PROFILES:
                        body = {"stage": sid, "from_lat": home_point["lat"],
                                "from_lon": home_point["lon"], "from_name": "自宅",
                                "to_lat": s["lat"], "to_lon": s["lon"], "to_name": s["name"],
                                "mode": prof, "date": snap["date"], "time": snap["time"],
                                "modes": ROUTE_MODES}
                        bld.save(f"{head}/routes/{hid}/{prof}/{safe_id(s['id'])}.json",
                                 bld.post("/api/routes", body))
                    if i % 20 == 0:
                        print(f"    経路 {i}/{len(targets)} 件"
                              f"（{time.time() - started:.0f}秒）", flush=True)
                print(f"  {snap['id']} / {home['name']}: 経路 {len(targets)}×{len(PROFILES)} 通り"
                      f"（{time.time() - started:.0f}秒）", flush=True)


def build_site(out: Path):
    """画面のファイルを置く。**静的版だと分かる印（config.js）**も書く。"""
    src = REPO / "frontend"
    out.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.iterdir()):
        if path.is_file():
            shutil.copy2(path, out / path.name)
    # **config.js を読む行は静的版にだけ足す。** 開発用の index.html は素のままにしておく
    # （Flask で開いたときに config.js が 404 になるのを避けるため）
    index = (out / "index.html")
    html = index.read_text(encoding="utf-8")
    if "config.js" not in html:
        html = html.replace('<script src="app.js">',
                            '<script src="config.js"></script>\n<script src="app.js">')
        index.write_text(html, encoding="utf-8")
    build_id = time.strftime("%Y%m%d-%H%M")
    (out / "config.js").write_text(
        "// 事前計算した結果を読む静的版（GitHub Pages）。\n"
        "// サーバ版（Flask）で開くときはこのファイルを読み込まない。\n"
        "window.KOSODATE_STATIC = true;\n"
        "window.KOSODATE_API_BASE = 'api';\n"
        # 作り直したときに、ブラウザや CDN の古いファイルを掴まないようにする
        f"window.KOSODATE_BUILD = '{build_id}';\n", encoding="utf-8")
    # GitHub Pages は _ で始まるファイルを Jekyll が無視するので、Jekyll 自体を止める
    (out / ".nojekyll").write_text("", encoding="utf-8")
    print(f"  画面のファイルを {out} に置きました")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(SITE))
    ap.add_argument("--only", choices=["all", "api", "site"], default="all")
    ap.add_argument("--stage", default=None, help="この舞台だけ作る")
    ap.add_argument("--radii", type=int, nargs="*", default=None,
                    help="作る半径（m）。省略すると画面のスライダと同じ刻み")
    ap.add_argument("--max-routes", type=int, default=None, help="経路を作る行き先の数を絞る")
    ap.add_argument("--skip-routes", action="store_true",
                    help="経路は作り直さない（到達圏やスポットだけ直したいとき）")
    ap.add_argument("--clean", action="store_true", help="先に出力先を空にする")
    args = ap.parse_args()

    out = Path(args.out)
    if not out.is_absolute():
        out = REPO / out
    if args.clean and out.exists():
        shutil.rmtree(out)

    if args.only in ("all", "site"):
        build_site(out)
    if args.only in ("all", "api"):
        import yaml
        snapshots = yaml.safe_load((REPO / "data" / "snapshots.yaml")
                                   .read_text(encoding="utf-8")) or []
        stages = stages_mod.load_all()
        client = app_mod.app.test_client()
        bld = Builder(client, out)
        started = time.time()
        build_api(bld, stages, snapshots, args.stage, args.radii, args.max_routes,
                  args.skip_routes)
        print(f"✅ {bld.files} ファイル / {bld.bytes / 1024 / 1024:.1f} MB "
              f"/ {time.time() - started:.0f} 秒 → {out}")


if __name__ == "__main__":
    main()
