#!/usr/bin/env python3.9
"""**ほこナビの段差・幅員・勾配・屋根を、歩行グラフのエッジに先に貼る。**

    python3.9 scripts/build_barrier_edges.py [stage_id]

入力: data/<stage>.walk.sqlite（OSM のグラフ）＋ data/raw/<stage>/hokonavi/
出力: 同じ sqlite の中に `barrier_edge` テーブル

**なぜ先に貼るのか。** 経路探索（ダイクストラ）は 1 回で何万本もの辺のコストを計算する。
そのたびにほこナビを座標で引くと間に合わない。**エッジ 1 本につき 1 回だけ**引いて
結果を持っておけば、探索中は辞書を見るだけで済む。

**当てはめ方**: OSM のエッジの中点から 25m 以内にある、いちばん近いほこナビ区間の属性を採る
（`backend/barrier.py` の `lookup()` と同じ考え方）。ほこナビの区間は歩道 1 本ごとに
引かれているので、車道の中心線に付いてしまうことがある。**車が通れる道には貼らない**
（`walk=1 かつ car=0`、つまり歩行者専用の道）ことで、そこを避けている。
"""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import barrier as barrier_mod       # noqa: E402
from backend import stages as stages_mod         # noqa: E402
from backend.walk import cell_of                 # noqa: E402

SCHEMA = """
DROP TABLE IF EXISTS barrier_edge;
CREATE TABLE barrier_edge (
  a INTEGER, b INTEGER, cell TEXT,
  lev_diff TEXT, width TEXT, vtcl_slope TEXT, roof TEXT, route_type TEXT
);
CREATE INDEX idx_barrier_cell ON barrier_edge(cell);
CREATE INDEX idx_barrier_ab ON barrier_edge(a, b);
"""


def build(stage):
    layer = barrier_mod.BarrierLayer(stage)
    db = stage.walk_db_path
    if not db.exists():
        print(f"  ⏭  {stage.name}: {db.name} がありません（先に build_walk.py）")
        return
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    if not layer.available:
        con.commit()
        print(f"  ⏭  {stage.name}: ほこナビのデータがありません（表は空のまま作った）")
        return
    # **ほこナビがある場所の周りだけを見る。** 区の全域を当たると無駄が大きい
    near_cells = set()
    for (cy, cx) in layer.index:
        lat = (cy + 0.5) * barrier_mod.CELL
        lon = (cx + 0.5) * barrier_mod.CELL
        for dy in (-0.002, 0, 0.002):
            for dx in (-0.002, 0, 0.002):
                near_cells.add(cell_of(lat + dy, lon + dx))
    cur = con.cursor()
    nodes = {r[0]: (r[1], r[2]) for r in cur.execute("SELECT id, lat, lon FROM node")}
    started = time.time()
    rows, looked, skipped_car = [], 0, 0
    q = ",".join("?" * len(near_cells))
    for a, b, cell, walk, car in cur.execute(
            f"SELECT a, b, cell, walk, car FROM edge WHERE cell IN ({q})", list(near_cells)):
        if not walk:
            continue
        if car:                      # 車が通れる道は歩道ではないので貼らない
            skipped_car += 1
            continue
        pa, pb = nodes.get(a), nodes.get(b)
        if not (pa and pb):
            continue
        looked += 1
        info = layer.lookup((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2)
        if not info:
            continue
        rows.append((a, b, cell, info["lev_diff"], info["width"],
                     info["vtcl_slope"], info["roof"], info["route_type"]))
    con.executemany("INSERT INTO barrier_edge VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    total = cur.execute("SELECT count(*) FROM edge").fetchone()[0]
    print(f"  {stage.name}: ほこナビ {layer.count} 区間 → 歩行グラフの {len(rows):,} 本に貼りました"
          f"（歩行者の道 {looked:,} 本を当たって {len(rows) / max(1, looked):.0%}／"
          f"車も通る道 {skipped_car:,} 本は対象外／グラフ全体は {total:,} 本）"
          f"（{time.time() - started:.0f}秒）")
    import collections
    c = collections.Counter(r[3] for r in rows)
    named = "・".join(f"{barrier_mod.LEV_DIFF.get(k, k)} {v}" for k, v in c.most_common())
    print(f"    貼った段差の内訳: {named}")


if __name__ == "__main__":
    all_stages = stages_mod.load_all()
    targets = [all_stages[sys.argv[1]]] if len(sys.argv) > 1 else list(all_stages.values())
    for stage in targets:
        build(stage)
