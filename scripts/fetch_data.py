#!/usr/bin/env python3.9
"""舞台ごとのオープンデータを取得して data/raw/<stage>/ に置く。

    python3.9 scripts/fetch_data.py [stage_id] [--only gtfs|hokonavi|spots]

取得するもの
  gtfs      … 時刻表（ODPT / gtfs-data.jp）。GTFS-Pathways もここ
  hokonavi  … 歩行空間ネットワーク（屋外）とバリアフリー施設（トイレ）
  spots     … 子育てスポットの元データ（自治体のカタログ）

アクセストークンは環境変数 ODPT_ACCESS_TOKEN、なければワークスペース直下の .env から読む。
**チャレンジ用トークンが要るフィード（required: false）は、無ければ飛ばして続ける。**
"""
import argparse
import gzip
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import stages as stages_mod  # noqa: E402

UA = {"User-Agent": "kosodate-navi (odc2026)"}
ODPT_PUBLIC = "https://api-public.odpt.org/api/v4/files/"
ODPT_TOKEN = "https://api.odpt.org/api/v4/files/"
ODPT_CHALLENGE = "https://api-challenge.odpt.org/api/v4/files/"
GTFS_JP_FILES = "https://api.gtfs-data.jp/v2/files"
HOKONAVI_API = "https://ckan.hokonavi.go.jp/api/3/action/"


def load_token(name="ODPT_ACCESS_TOKEN"):
    token = os.environ.get(name)
    if token:
        return token
    here = Path(__file__).resolve()
    for env_path in (here.parent.parent / ".env", here.parents[3] / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    return None


def get(url, timeout=600):
    req = urllib.request.Request(url, headers=UA)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as res:
        data = res.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def save(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def unzip(zip_path: Path, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out_dir)


def fetch_gtfs(stage, force=False):
    out = []
    token = load_token()
    challenge = load_token("ODPT_CHALLENGE_TOKEN")
    jp_index = None
    for feed in stage.transit:
        key = feed["id"]
        dest = stage.raw_dir / "gtfs" / key
        zip_path = stage.raw_dir / "gtfs" / f"{key}.zip"
        src = feed.get("src")
        try:
            if src == "odpt_public":
                url = ODPT_PUBLIC + feed["path"]
            elif src == "odpt_token":
                if not token:
                    raise RuntimeError("ODPT_ACCESS_TOKEN がありません")
                url = ODPT_TOKEN + feed["path"] + "?acl:consumerKey=" + urllib.parse.quote(token)
            elif src == "odpt_challenge":
                if not challenge:
                    raise RuntimeError("チャレンジ用トークン（ODPT_CHALLENGE_TOKEN）がありません")
                url = ODPT_CHALLENGE + feed["path"] + "?acl:consumerKey=" + urllib.parse.quote(challenge)
            elif src == "odpt_bus_json":
                # バスの JSON API（座標が無い）を GTFS に組み直して zip にする。
                # 中身は backend/odptbus.py。ここでは他のフィードと同じ形にして渡す
                if not challenge:
                    raise RuntimeError("チャレンジ用トークン（ODPT_CHALLENGE_TOKEN）がありません")
                if force or not zip_path.exists():
                    from backend import odptbus
                    odptbus.build_zip(stage, feed, challenge, zip_path)
                url = None
            elif src == "gtfs_data_jp":
                if jp_index is None:
                    jp_index = json.loads(get(GTFS_JP_FILES).decode("utf-8"))["body"]
                org, _, feed_id = feed["path"].partition("/feeds/")
                hit = [f for f in jp_index
                       if f["organization_id"] == org and f["feed_id"] == feed_id]
                if not hit:
                    raise RuntimeError("gtfs-data.jp に見つかりません")
                url = hit[0]["file_url"]
                feed.setdefault("from_date", hit[0].get("file_from_date"))
                feed.setdefault("to_date", hit[0].get("file_to_date"))
            else:
                raise RuntimeError(f"未知の src: {src}")

            if url and (force or not zip_path.exists()):
                save(zip_path, get(url))
            unzip(zip_path, dest)
            out.append(feed)
            print(f"  ✅ {key}: {zip_path.stat().st_size:,} bytes")
        except Exception as exc:                     # noqa: BLE001
            if feed.get("required", True):
                print(f"  ❌ {key}: {exc}")
            else:
                print(f"  ⏭  {key}: {exc}（required: false なので飛ばす）")
    index = stage.raw_dir / "gtfs" / "index.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def hokonavi_package(name):
    url = HOKONAVI_API + "package_show?" + urllib.parse.urlencode({"id": name})
    return json.loads(get(url).decode("utf-8"))["result"]


def fetch_hokonavi(stage, force=False):
    layers = [l for l in stage.walk_layers if l.get("kind") == "hokonavi"]
    for layer in layers:
        for name in layer.get("datasets", []):
            pkg = hokonavi_package(name)
            for res in pkg["resources"]:
                if (res.get("format") or "").upper() != "CSV":
                    continue
                rname = (res.get("name") or "").lower()
                which = "node" if "node" in rname else ("link" if "link" in rname else None)
                if not which:
                    continue
                path = stage.raw_dir / "hokonavi" / name / f"{which}.csv"
                if force or not path.exists():
                    save(path, get(res["url"]))
            print(f"  ✅ ほこナビ {name}")


def fetch_spots(stage, force=False):
    for spot in stage.spots:
        url = spot.get("url")
        if not url:
            continue
        path = stage.raw_dir / "spots" / f"{spot['id']}.csv"
        if force or not path.exists():
            try:
                save(path, get(url))
                print(f"  ✅ {spot['id']}: {path.stat().st_size:,} bytes")
            except Exception as exc:                 # noqa: BLE001
                print(f"  ❌ {spot['id']}: {exc}")
        else:
            print(f"  ・ {spot['id']}: 取得済み")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", nargs="?")
    ap.add_argument("--only", choices=["gtfs", "hokonavi", "spots"])
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    all_stages = stages_mod.load_all()
    targets = [all_stages[args.stage]] if args.stage else list(all_stages.values())
    for stage in targets:
        print(f"== {stage.name} ==")
        if args.only in (None, "gtfs"):
            print(" GTFS")
            fetch_gtfs(stage, args.force)
        if args.only in (None, "hokonavi"):
            print(" ほこナビ")
            fetch_hokonavi(stage, args.force)
        if args.only in (None, "spots"):
            print(" スポット")
            fetch_spots(stage, args.force)


if __name__ == "__main__":
    main()
