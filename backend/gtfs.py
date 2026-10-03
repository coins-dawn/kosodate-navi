"""GTFS（と GTFS-Pathways）を SQLite に正規化する。

- フィードをまたぐと ID が衝突するので、どのテーブルにも feed_id を持たせる。
- GTFS-Pathways は「駅の中」の歩行グラフなので、通常の停留所とは別のテーブルに入れる
  （pathway / pathway_stop）。stop_times が参照するのは location_type=0 の「乗り場」で、
  出入口(2) → 通路(3) → 乗降場所(4) → 乗り場(0) → 駅(1) という親子関係になっている。
"""
import csv
import json
import os
import shutil
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SCHEMA = """

CREATE TABLE feed (
  feed_id TEXT PRIMARY KEY, organization TEXT, feed_name TEXT,
  license TEXT, license_url TEXT, from_date TEXT, to_date TEXT, kind TEXT
);

CREATE TABLE stop (
  feed_id TEXT, stop_id TEXT, stop_name TEXT, lat REAL, lon REAL,
  location_type INTEGER, parent_station TEXT, wheelchair_boarding TEXT,
  PRIMARY KEY (feed_id, stop_id)
);
CREATE INDEX idx_stop_name ON stop(stop_name);

CREATE TABLE route (
  feed_id TEXT, route_id TEXT, agency_id TEXT, short_name TEXT, long_name TEXT,
  route_type INTEGER, PRIMARY KEY (feed_id, route_id)
);

CREATE TABLE trip (
  feed_id TEXT, trip_id TEXT, route_id TEXT, service_id TEXT, headsign TEXT,
  shape_id TEXT, wheelchair_accessible TEXT,
  PRIMARY KEY (feed_id, trip_id)
);
CREATE INDEX idx_trip_route ON trip(feed_id, route_id);

CREATE TABLE stop_time (
  feed_id TEXT, trip_id TEXT, seq INTEGER, stop_id TEXT, arr INTEGER, dep INTEGER
);
CREATE INDEX idx_st_trip ON stop_time(feed_id, trip_id, seq);
CREATE INDEX idx_st_stop ON stop_time(feed_id, stop_id, dep);

CREATE TABLE calendar (
  feed_id TEXT, service_id TEXT,
  mon INTEGER, tue INTEGER, wed INTEGER, thu INTEGER, fri INTEGER, sat INTEGER, sun INTEGER,
  start_date TEXT, end_date TEXT, PRIMARY KEY (feed_id, service_id)
);

CREATE TABLE calendar_date (
  feed_id TEXT, service_id TEXT, date TEXT, exception_type INTEGER
);
CREATE INDEX idx_caldate ON calendar_date(feed_id, date);

CREATE TABLE shape_point (
  feed_id TEXT, shape_id TEXT, seq INTEGER, lat REAL, lon REAL
);
CREATE INDEX idx_shape ON shape_point(feed_id, shape_id, seq);

CREATE TABLE fare_attribute (
  feed_id TEXT, fare_id TEXT, price REAL, currency TEXT, PRIMARY KEY (feed_id, fare_id)
);
CREATE TABLE fare_rule (
  feed_id TEXT, fare_id TEXT, route_id TEXT, origin_id TEXT, destination_id TEXT
);

-- 駅の中（GTFS-Pathways）
CREATE TABLE pathway_stop (
  feed_id TEXT, stop_id TEXT, stop_name TEXT, lat REAL, lon REAL,
  location_type INTEGER, parent_station TEXT, level_id TEXT,
  PRIMARY KEY (feed_id, stop_id)
);
CREATE TABLE pathway (
  feed_id TEXT, pathway_id TEXT, from_stop TEXT, to_stop TEXT,
  mode INTEGER, bidirectional INTEGER, length REAL
);
CREATE INDEX idx_pathway_from ON pathway(feed_id, from_stop);
"""


def read_csv(base: Path, name: str):
    cands = [base / name, base / name.replace(".txt", ".csv")]
    cands += list(base.glob(f"*/{name}")) + list(base.glob(f"*/{name.replace('.txt', '.csv')}"))
    for cand in cands:
        if cand.exists():
            with cand.open(encoding="utf-8-sig", newline="") as f:
                return list(csv.DictReader(f))
    return []


def to_sec(value):
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def load_feed(con, meta, base: Path):
    feed_id = meta["id"]
    con.execute("INSERT OR REPLACE INTO feed VALUES (?,?,?,?,?,?,?,?)", (
        feed_id, meta.get("organization"), meta.get("feed_name"), meta.get("license"),
        meta.get("license_url"), meta.get("from_date"), meta.get("to_date"), meta.get("kind", "gtfs")))

    con.executemany("INSERT OR REPLACE INTO stop VALUES (?,?,?,?,?,?,?,?)", [
        (feed_id, r["stop_id"], r.get("stop_name"), float(r["stop_lat"]), float(r["stop_lon"]),
         _int(r.get("location_type")), r.get("parent_station") or None,
         (r.get("wheelchair_boarding") or "").strip())
        for r in read_csv(base, "stops.txt") if r.get("stop_lat")])

    con.executemany("INSERT OR REPLACE INTO route VALUES (?,?,?,?,?,?)", [
        (feed_id, r["route_id"], r.get("agency_id") or "", r.get("route_short_name"),
         r.get("route_long_name"), _int(r.get("route_type"), 3))
        for r in read_csv(base, "routes.txt")])

    con.executemany("INSERT OR REPLACE INTO trip VALUES (?,?,?,?,?,?,?)", [
        (feed_id, r["trip_id"], r["route_id"], r["service_id"], r.get("trip_headsign"),
         r.get("shape_id"), (r.get("wheelchair_accessible") or "").strip())
        for r in read_csv(base, "trips.txt")])

    con.executemany("INSERT INTO stop_time VALUES (?,?,?,?,?,?)", [
        (feed_id, r["trip_id"], _int(r["stop_sequence"]), r["stop_id"],
         to_sec(r.get("arrival_time")), to_sec(r.get("departure_time")))
        for r in read_csv(base, "stop_times.txt") if r.get("stop_id")])

    con.executemany("INSERT OR REPLACE INTO calendar VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        (feed_id, r["service_id"], _int(r["monday"]), _int(r["tuesday"]), _int(r["wednesday"]),
         _int(r["thursday"]), _int(r["friday"]), _int(r["saturday"]), _int(r["sunday"]),
         r.get("start_date"), r.get("end_date"))
        for r in read_csv(base, "calendar.txt")])

    con.executemany("INSERT INTO calendar_date VALUES (?,?,?,?)", [
        (feed_id, r["service_id"], r["date"], _int(r["exception_type"]))
        for r in read_csv(base, "calendar_dates.txt")])

    con.executemany("INSERT INTO shape_point VALUES (?,?,?,?,?)", [
        (feed_id, r["shape_id"], _int(r["shape_pt_sequence"]),
         float(r["shape_pt_lat"]), float(r["shape_pt_lon"]))
        for r in read_csv(base, "shapes.txt") if r.get("shape_pt_lat")])

    con.executemany("INSERT OR REPLACE INTO fare_attribute VALUES (?,?,?,?)", [
        (feed_id, r["fare_id"], float(r["price"]), r.get("currency_type"))
        for r in read_csv(base, "fare_attributes.txt") if r.get("price")])

    con.executemany("INSERT INTO fare_rule VALUES (?,?,?,?,?)", [
        (feed_id, r["fare_id"], r.get("route_id"), r.get("origin_id"), r.get("destination_id"))
        for r in read_csv(base, "fare_rules.txt")])


def load_pathways(con, meta, base: Path):
    """駅の中。stops は出入口・通路・乗降場所・乗り場・駅がぜんぶ入っている。"""
    feed_id = meta["id"]
    con.execute("INSERT OR REPLACE INTO feed VALUES (?,?,?,?,?,?,?,?)", (
        feed_id, meta.get("organization"), meta.get("feed_name"), meta.get("license"),
        meta.get("license_url"), None, None, "pathways"))

    con.executemany("INSERT OR REPLACE INTO pathway_stop VALUES (?,?,?,?,?,?,?,?)", [
        (feed_id, r["stop_id"], r.get("stop_name"),
         float(r["stop_lat"]) if r.get("stop_lat") else None,
         float(r["stop_lon"]) if r.get("stop_lon") else None,
         _int(r.get("location_type")), r.get("parent_station") or None, r.get("level_id"))
        for r in read_csv(base, "stops.txt")])

    con.executemany("INSERT INTO pathway VALUES (?,?,?,?,?,?,?)", [
        (feed_id, r["pathway_id"], r["from_stop_id"], r["to_stop_id"],
         _int(r.get("pathway_mode")), _int(r.get("is_bidirectional")),
         float(r["length"]) if r.get("length") else None)
        for r in read_csv(base, "pathways.txt")])


# 共有フォルダ（/vagrant）の上で大きな SQLite を書くと mmap で落ちる（Bus error）。
# 構築はローカルディスクで行い、できたファイルを data/ に持ってくる。
BUILD_DIR = Path(os.environ.get("KOSODATE_BUILD_DIR", "/var/tmp/kosodate-navi"))


def build(stage):
    db_path = stage.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = BUILD_DIR / f"{stage.id}.build.sqlite"
    for p in (tmp_path, db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        if p.exists():
            p.unlink()
    con = sqlite3.connect(tmp_path)
    con.executescript(SCHEMA)

    index_path = stage.raw_dir / "gtfs" / "index.json"
    feeds = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    for meta in feeds:
        base = stage.raw_dir / "gtfs" / meta["id"]
        if not base.exists():
            continue
        if meta.get("kind") == "pathways":
            load_pathways(con, meta, base)
        else:
            load_feed(con, meta, base)
    con.commit()
    con.close()
    shutil.copy(tmp_path, db_path)
    tmp_path.unlink()
    return connect(db_path)


def connect(db_path: Path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con
