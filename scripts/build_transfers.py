#!/usr/bin/env python3.9
"""停留所どうしの乗換時間を、歩行グラフから事前計算する。

    python3.9 scripts/build_transfers.py [stage_id]

年齢ごとに計算すると爆発するので、プロファイルを階級（tier）に丸めて 1 セットずつ作る。
  stroller … 階段を通れない（0〜3歳）
  normal   … 標準（6〜12歳）
  gentle   … 歩く速さが遅い（産前・3〜6歳）※ normal の結果を速さの比で伸ばす
"""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import gtfs, profile as profile_mod, stages as stages_mod, transit, walk  # noqa: E402

MAX_TRANSFER_M = 400
MAX_TRANSFER_SEC = 600
TIERS = {"stroller": 12, "normal": 96}      # 階級 → 代表の月齢

SCHEMA = """
CREATE TABLE transfer (tier TEXT, a TEXT, b TEXT, seconds INTEGER);
CREATE INDEX idx_transfer ON transfer(tier, a);
"""


def build(stage):
    con = gtfs.connect(stage.db_path)
    net = transit.Network(con)
    graph = walk.WalkGraph(stage.walk_db_path)
    if not graph.available:
        print("  歩行グラフがありません。先に build_walk.py を実行してください")
        return

    x1, y1, x2, y2 = stage.bbox()
    m = 5.0 / 111.0
    places = [(pid, p["lat"], p["lon"]) for pid, p in net.places.items()
              if y1 - m <= p["lat"] <= y2 + m and x1 - m * 1.25 <= p["lon"] <= x2 + m * 1.25]
    print(f"  対象の場所 {len(places):,}（全 {len(net.places):,}）")

    out_path = stage.transfer_db_path
    tmp = Path("/var/tmp/kosodate-navi") / f"{stage.id}.transfers.build.sqlite"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    if tmp.exists():
        tmp.unlink()
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)

    speeds = {}
    for tier, months in TIERS.items():
        prof = profile_mod.for_months(months)
        speeds[tier] = prof.walk_speed
        rows = []
        started = time.time()
        for i, (pid, lat, lon) in enumerate(places):
            near = [(p2, la2, lo2) for p2, la2, lo2 in places
                    if p2 != pid and abs(la2 - lat) < 0.005
                    and transit.haversine(lat, lon, la2, lo2) <= MAX_TRANSFER_M]
            if not near:
                continue
            got = graph.reachable_points((lat, lon), near, prof, "walk", MAX_TRANSFER_SEC)
            for p2, sec in got.items():
                rows.append((tier, pid, p2, sec))
            if i % 200 == 0 and i:
                print(f"    {tier} {i}/{len(places)} 経過 {time.time() - started:.0f}s "
                      f"（{len(rows):,} 組）", flush=True)
            if len(graph.loaded_cells) > 900:      # 読み込みすぎたら一度捨てる
                graph.nodes.clear(); graph.adj.clear(); graph.node_tags.clear()
                graph._cell_nodes.clear(); graph.loaded_cells.clear()
        db.executemany("INSERT INTO transfer VALUES (?,?,?,?)", rows)
        print(f"  {tier}: {len(rows):,} 組（{time.time() - started:.0f}s）")

    # gentle は normal を歩く速さの比で伸ばす
    gentle_speed = profile_mod.for_months(-3).walk_speed
    ratio = speeds["normal"] / gentle_speed
    db.execute("INSERT INTO transfer SELECT 'gentle', a, b, CAST(seconds * ? AS INTEGER) "
               "FROM transfer WHERE tier='normal'", (ratio,))
    db.commit()
    n = db.execute("SELECT COUNT(*) FROM transfer").fetchone()[0]
    db.close()
    if out_path.exists():
        out_path.unlink()
    import shutil
    shutil.copy(tmp, out_path)
    tmp.unlink()
    print(f"  ✅ 合計 {n:,} 組 → {out_path}")


def main():
    all_stages = stages_mod.load_all()
    targets = [all_stages[sys.argv[1]]] if len(sys.argv) > 1 else list(all_stages.values())
    for stage in targets:
        print(f"== {stage.name} ==")
        build(stage)


if __name__ == "__main__":
    main()
