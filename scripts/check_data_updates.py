#!/usr/bin/env python3.9
"""**時刻表データが更新されていないか調べる。**

    python3.9 scripts/check_data_updates.py            # 全部の舞台を調べる
    python3.9 scripts/check_data_updates.py nagareyama

**なぜ要るか**: 公共交通オープンデータ開発者ガイドライン 2.2.2（定期的な更新）は、
静的データを表示するときに「本 API を利用して定期的に最新データを取得し、更新を行う」
「センターがデータ更新の通知を行ってから **1 週間以内**に更新する」ことを求めている。
基本ライセンス第4条2項(4) にも「更新された場合はガイドラインに従って成果物を直ちに更新」とある。

このサービスは**結果を先に計算して配る静的サイト**なので、元データが変わっても
自動では追随しない。**通知を待つだけでなく、こちらから見に行く**ためのスクリプト。

やっていること: 配信元に**1 バイトだけ**取りに行き（Range: bytes=0-0）、
`Last-Modified` とファイル全体の大きさを手元の zip と比べる。

- 大きさが違う → **中身が変わっている**
- 大きさは同じで Last-Modified だけ新しい → 本体を取って中身を突き合わせる
  （ODPT は中身が同じでも配信し直すことがあり、日付だけでは空振りするため）

更新があったときは fetch_data.py --force → build_db.py → build_transfers.py
→ build_static.py の順に流し直す。
"""
import hashlib
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import stages as stages_mod  # noqa: E402
import fetch_data                          # noqa: E402  (URL の組み立てを共有する)

UA = {"User-Agent": "kosodate-navi (odc2026)"}


def feed_url(feed, tokens, jp_index):
    """fetch_data.py と同じ URL を組み立てる。**トークンは表示しない。**"""
    src = feed.get("src")
    if src == "odpt_public":
        return fetch_data.ODPT_PUBLIC + feed["path"]
    if src in ("odpt_token", "odpt_challenge"):
        key = "ODPT_ACCESS_TOKEN" if src == "odpt_token" else "ODPT_CHALLENGE_TOKEN"
        token = tokens.get(key)
        if not token:
            raise RuntimeError(f"{key} がありません")
        base = fetch_data.ODPT_TOKEN if src == "odpt_token" else fetch_data.ODPT_CHALLENGE
        return base + feed["path"] + "?acl:consumerKey=" + urllib.parse.quote(token)
    if src == "odpt_bus_json":
        # JSON API から組み直したもの（zip は手元で作っている）。
        # 配信側に比べる相手が無いので、ここでは見に行かない
        return None
    if src == "gtfs_data_jp":
        org, _, feed_id = feed["path"].partition("/feeds/")
        hit = [f for f in jp_index() if f["organization_id"] == org and f["feed_id"] == feed_id]
        if not hit:
            raise RuntimeError("gtfs-data.jp に見つかりません")
        return hit[0]["file_url"]
    raise RuntimeError(f"未知の src: {src}")


def peek(url):
    """1 バイトだけ取って、更新日時とファイル全体の大きさを見る。"""
    req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=120, context=ssl.create_default_context()) as res:
        total = res.headers.get("Content-Range", "").rsplit("/", 1)[-1]
        return (res.headers.get("Last-Modified"),
                int(total) if total.isdigit() else None)


def verdict(local_size, local_sha, remote_size, remote_newer, same_body=None):
    """更新されたかどうかの判定。**ここだけはネットワークなしで試せるように切り出す。**"""
    if local_size is None:
        return "手元に無い"
    if remote_size is not None and remote_size != local_size:
        return "更新あり"
    if not remote_newer:
        return "最新"
    if same_body is None:
        return "要確認"                      # 大きさが同じで日付だけ新しい
    return "最新（配信し直しただけ）" if same_body else "更新あり"


def check(stage, tokens, jp_index):
    rows = []
    for feed in stage.transit:
        zip_path = stage.raw_dir / "gtfs" / f"{feed['id']}.zip"
        local_size = zip_path.stat().st_size if zip_path.exists() else None
        local_time = zip_path.stat().st_mtime if zip_path.exists() else 0
        try:
            url = feed_url(feed, tokens, jp_index)
            if url is None:
                rows.append((feed.get("feed_name") or feed["id"],
                             time.strftime("%Y-%m-%d", time.localtime(local_time))
                             if local_size else "—", "—",
                             "組み直したもの（fetch_data.py --force で作り直す）"))
                continue
            last_mod, remote_size = peek(url)
        except Exception as exc:                     # noqa: BLE001
            rows.append((feed.get("feed_name") or feed["id"], "—", "—",
                         f"調べられない（{exc}）"))
            continue
        remote_epoch = (time.mktime(time.strptime(last_mod, "%a, %d %b %Y %H:%M:%S %Z"))
                        - time.timezone) if last_mod else 0
        remote_newer = remote_epoch > local_time
        same_body = None
        if local_size == remote_size and remote_newer and zip_path.exists():
            # 大きさが同じでも中身が違うことはある。本体を取って突き合わせる。
            # 配信は Azure の署名つき URL に転送されるしくみで、**たまに 403 を返す**ので数回試す
            body = None
            for attempt in range(3):
                try:
                    body = fetch_data.get(url)
                    break
                except Exception:                    # noqa: BLE001
                    time.sleep(3 * (attempt + 1))
            if body is None:
                rows.append((feed.get("feed_name") or feed["id"],
                             time.strftime("%Y-%m-%d", time.localtime(local_time)),
                             time.strftime("%Y-%m-%d", time.localtime(remote_epoch)),
                             "要確認（本体が取れなかった）"))
                continue
            same_body = (hashlib.sha256(body).hexdigest()
                         == hashlib.sha256(zip_path.read_bytes()).hexdigest())
        rows.append((feed.get("feed_name") or feed["id"],
                     time.strftime("%Y-%m-%d", time.localtime(local_time)) if local_size else "—",
                     time.strftime("%Y-%m-%d", time.localtime(remote_epoch)) if last_mod else "—",
                     verdict(local_size, None, remote_size, remote_newer, same_body)))
    return rows


def main():
    all_stages = stages_mod.load_all()
    targets = [all_stages[sys.argv[1]]] if len(sys.argv) > 1 else list(all_stages.values())
    tokens = {k: fetch_data.load_token(k)
              for k in ("ODPT_ACCESS_TOKEN", "ODPT_CHALLENGE_TOKEN")}
    cache = {}

    def jp_index():
        if "jp" not in cache:
            cache["jp"] = json.loads(
                fetch_data.get(fetch_data.GTFS_JP_FILES).decode("utf-8"))["body"]
        return cache["jp"]

    stale = 0
    for stage in targets:
        print(f"== {stage.name} ==")
        for name, mine, theirs, state in check(stage, tokens, jp_index):
            mark = "🔄" if state == "更新あり" else ("⚠️ " if state == "要確認" else "  ")
            print(f" {mark} {name}: 手元 {mine} / 配信側 {theirs} → {state}")
            stale += state in ("更新あり", "要確認")
    if stale:
        print(f"\n{stale} 件が更新されています。ガイドライン 2.2.2 は"
              "「更新の通知から 1 週間以内」を求めています。")
        print("  python3.9 scripts/fetch_data.py --force <stage>   # 取り直す")
        print("  python3.9 scripts/build_db.py <stage> ／ build_transfers.py <stage>")
        print("  python3.9 scripts/build_static.py                 # site/ を作り直す")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
