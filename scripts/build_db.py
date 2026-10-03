#!/usr/bin/env python3.9
"""取得した GTFS を SQLite に正規化する。

    python3.9 scripts/build_db.py [stage_id]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import gtfs, stages as stages_mod  # noqa: E402


def main():
    all_stages = stages_mod.load_all()
    targets = [all_stages[sys.argv[1]]] if len(sys.argv) > 1 else list(all_stages.values())
    for stage in targets:
        con = gtfs.build(stage)
        cur = con.cursor()
        print(f"== {stage.name} ==")
        # カーソルを使い回しながら中で問い合わせると反復が途切れるので、先に取り切る
        feeds = cur.execute(
            "SELECT feed_id, feed_name, kind FROM feed ORDER BY feed_id").fetchall()
        for feed_id, name, kind in feeds:
            if kind == "pathways":
                n_stop = cur.execute(
                    "SELECT COUNT(*) FROM pathway_stop WHERE feed_id=?", (feed_id,)).fetchone()[0]
                n_pw = cur.execute(
                    "SELECT COUNT(*) FROM pathway WHERE feed_id=?", (feed_id,)).fetchone()[0]
                print(f"  {name}: 地点 {n_stop:,} / 通路 {n_pw:,}")
            else:
                n_stop = cur.execute(
                    "SELECT COUNT(*) FROM stop WHERE feed_id=?", (feed_id,)).fetchone()[0]
                n_trip = cur.execute(
                    "SELECT COUNT(*) FROM trip WHERE feed_id=?", (feed_id,)).fetchone()[0]
                print(f"  {name}: 停留所 {n_stop:,} / 便 {n_trip:,}")
        con.close()


if __name__ == "__main__":
    main()
