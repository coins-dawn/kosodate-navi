# 子育てナビ（仮）

子ども連れで出かける先と、そこまでの行き方を地図で見るための web アプリ。

公開物は `site/`（静的ファイルだけ）で、GitHub Pages に置いている。

```bash
python3.9 scripts/build_static.py --clean     # site/ を作り直す（約 35 分）
```

## 動かす

```bash
python3.9 -m pip install -r requirements.txt

# データを用意する（初回のみ。合わせて 30 分ほど）
python3.9 scripts/build_boundary.py          # 行政区域（N03）
python3.9 scripts/fetch_data.py              # GTFS・ほこナビ・自治体カタログ
python3.9 scripts/build_db.py                # GTFS → SQLite
python3.9 scripts/build_walk.py              # OSM → 歩行グラフ（osmium が要る）
python3.9 scripts/build_barrier_edges.py     # ほこナビの段差などを歩行グラフの辺に貼る
python3.9 scripts/build_transfers.py         # 乗換表（歩行グラフから）
python3.9 scripts/build_spots.py             # スポットの正規化
python3.9 scripts/build_rail_shapes.py       # 線路の形（OSM → data/rail/*.geojson）
python3.9 scripts/build_busstop_geo.py       # バス停の座標表（OSM → data/busstop/*.json）

# スポットの概要と公式ページ（独自整備・いまは流山市だけ）
python3.9 scripts/fetch_city_pages.py
python3.9 scripts/fetch_city_pages.py --section child
python3.9 scripts/build_spot_details.py nagareyama
python3.9 scripts/build_spots.py nagareyama

# 市外のスポット
python3.9 scripts/build_osm_spots.py nagareyama
python3.9 scripts/build_spots.py nagareyama

bash scripts/run.sh                          # http://localhost:8002（開発用）

python3.9 scripts/check_data_updates.py      # 配信データの更新を見に行く
```

テスト: `python3.9 -m pytest tests/ -q`

`ODPT_ACCESS_TOKEN` と `ODPT_CHALLENGE_TOKEN` はワークスペース直下の `.env` から読む
（リポジトリには入れていない）。

## 作り

| 層 | 採用 |
|---|---|
| バックエンド | Flask（Python 3.9）＋ SQLite |
| 公共交通の探索 | 自前（RAPTOR の簡易版） |
| 徒歩 | 自前グラフ＋Dijkstra |
| OSM の取り込み | osmium でローカルの pbf を切る |
| フロントエンド | 素の JS ＋ MapLibre GL JS ＋ OpenFreeMap positron（鍵不要） |

## データの出典

公共交通オープンデータセンター（ODPT）／GTFSデータリポジトリ（gtfs-data.jp）／
歩行空間ナビ・プロジェクト（国土交通省）／東京都オープンデータカタログサイト／
流山市オープンデータ／港区オープンデータ／国土数値情報 行政区域／
OpenStreetMap contributors／OpenFreeMap（OpenMapTiles）／気象庁

**時刻表には、再配布に制限のあるライセンスのデータが含まれる。**
フォークして公開し直す場合は、各データのライセンスを確認すること
（画面の「このサービスについて」に、フィードごとのライセンスと取得日を出している）。
