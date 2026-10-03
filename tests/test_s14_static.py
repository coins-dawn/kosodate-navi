"""S14 静的化（GitHub Pages）。**出発時刻を固定して、結果を先に計算して配る。**

サーバ版は 1 リクエストで最大 1.5GB のメモリを使う（新宿区の `stop_time` 116 万行）。
無料のホスティングは 512MB 級なので載らない。出発時刻を決めれば答えは有限なので、
`scripts/build_static.py` が API を呼んで JSON として書き出す。

ここで確かめるのは、**静的版とサーバ版で同じ答えが返ること**と、
**画面が探しに行くファイル名の形**（frontend/app.js の `API` と合っていること）。
"""
import json
import subprocess
import sys

import pytest

from backend import stages as stages_mod


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """いちばん小さい組み合わせで実際に作ってみる（流山・半径1km・行き先2件）。"""
    out = tmp_path_factory.mktemp("site")
    cmd = [sys.executable, "scripts/build_static.py", "--out", str(out),
           "--stage", "nagareyama", "--radii", "1000", "--max-routes", "2"]
    res = subprocess.run(cmd, cwd=str(stages_mod.REPO_ROOT),
                         capture_output=True, text=True, timeout=1800)
    assert res.returncode == 0, res.stdout + res.stderr
    return out


def test_site_has_the_frontend_and_the_static_flag(built):
    """画面のファイルが入っていて、**静的版だと分かる印**が付いていること。"""
    for name in ("index.html", "app.js", "style.css", "config.js", ".nojekyll"):
        assert (built / name).exists(), name
    config = (built / "config.js").read_text(encoding="utf-8")
    assert "KOSODATE_STATIC = true" in config
    # 開発用の index.html は素のまま。config.js を読む行は静的版にだけ足す
    assert "config.js" in (built / "index.html").read_text(encoding="utf-8")
    assert "config.js" not in (stages_mod.REPO_ROOT / "frontend" / "index.html").read_text(
        encoding="utf-8")


def test_file_layout_matches_what_the_screen_asks_for(built):
    """`frontend/app.js` の `API` が組み立てるパスと同じ形で置かれていること。"""
    snaps = json.loads((built / "api" / "snapshots.json").read_text(encoding="utf-8"))
    assert snaps and snaps[0]["id"] and snaps[0]["date"] and snaps[0]["time"]
    snap = snaps[0]["id"]
    assert (built / "api" / "stages.json").exists()
    for name in ("boundary", "stations", "coverage"):
        assert (built / "api" / "nagareyama" / f"{name}.json").exists()
    assert (built / "api" / "nagareyama" / snap / "events.json").exists()
    home = "otakanomori"
    assert (built / "api" / "nagareyama" / snap / "nearby" / home / "1000.json").exists()
    for prof in ("walk", "stroller"):
        got = list((built / "api" / "nagareyama" / snap / "routes" / home / prof).glob("*.json"))
        assert len(got) == 2, prof
    # スポットの吹き出しは**さがす範囲に入らないものも含めて全部**作る
    spots = list((built / "api" / "nagareyama" / "spot").glob("*.json"))
    assert len(spots) > 400
    assert not any(":" in p.name for p in spots)      # ファイル名に使えない字は置き換える


def test_precomputed_nearby_is_the_same_as_the_server(built, client):
    """**静的版はサーバ版と同じ答え**（同じ API を呼んで保存しているので当然そうなるべき）。"""
    snap = json.loads((built / "api" / "snapshots.json").read_text(encoding="utf-8"))[0]
    saved = json.loads((built / "api" / "nagareyama" / snap["id"] / "nearby" / "otakanomori"
                        / "1000.json").read_text(encoding="utf-8"))
    live = client.get(f"/api/nearby?stage=nagareyama&home=otakanomori&radius_m=1000"
                      f"&date={snap['date']}&time={snap['time']}").get_json()
    assert saved["radius_m"] == live["radius_m"] == 1000
    assert saved["count"] == live["count"]
    assert [s["id"] for s in saved["spots"]] == [s["id"] for s in live["spots"]]
    assert saved["inside"] == live["inside"] and saved["outside"] == live["outside"]


def test_routes_hold_every_mode_in_one_file(built):
    """静的版の経路は**1 ファイルに画面の 4 通りとも**入れる（画面が 1 回で出せるように）。

    **自転車と車は事前計算しない**（2026-09-30・ユーザー指示）。公共交通ではなく、
    推奨されているオープンデータも使っていないため。
    """
    snap = json.loads((built / "api" / "snapshots.json").read_text(encoding="utf-8"))[0]
    got = sorted((built / "api" / "nagareyama" / snap["id"] / "routes" / "otakanomori"
                  / "walk").glob("*.json"))
    d = json.loads(got[0].read_text(encoding="utf-8"))
    assert set(d) >= {"total", "train", "bus", "walk"}
    assert "bike" not in d and "car" not in d
    assert d["date"] == snap["date"]


def test_weather_is_not_baked_in(built):
    """**天気は焼き込まない。** 数日後に「今日の天気」が嘘になるため。

    静的版はブラウザから気象庁の JSON を直接取る（`frontend/app.js` の `forecastOf`）。
    """
    spots = sorted((built / "api" / "nagareyama" / "spot").glob("*.json"))
    for path in spots[:20]:
        d = json.loads(path.read_text(encoding="utf-8"))
        assert d["weather"] is None
        assert d["advice"] == []
