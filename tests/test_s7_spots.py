"""S7 スポット。年齢で出る件数が変わることと、データの穴を数えていること。"""
from backend import profile, spots


def count(index, months, lat, lon):
    _, total = index.search(lat, lon, profile.for_months(months))
    return total


def test_age_changes_the_map(nagareyama):
    idx = spots.SpotIndex(nagareyama)
    lat, lon = 35.8562, 139.9029
    assert count(idx, 6, lat, lon) > count(idx, 96, lat, lon)
    assert count(idx, -3, lat, lon) < count(idx, 6, lat, lon)


def test_nagareyama_sources(nagareyama):
    idx = spots.SpotIndex(nagareyama)
    by = {s["id"]: s for s in idx.coverage()["sources"]}
    assert by["nagareyama_hotspace"]["count"] == 64
    assert by["nagareyama_park"]["count"] == 429
    # 91 件のうち 70 件は入園・入会が要る施設なので取り込んでいない
    assert by["nagareyama_childcare"]["count"] == 21
    assert by["nagareyama_childcare"]["enrollment_excluded"] == 70


def test_curated_spots_have_sources(nagareyama):
    idx = spots.SpotIndex(nagareyama)
    local = [s for s in idx.spots if s["source_id"].endswith("_local")]
    assert local
    for s in local:
        assert s["source_url"] and s["checked_at"]


def test_enrollment_places_are_not_in_the_data(nagareyama):
    """保育所・幼稚園・学童は「今日どこかに行きたい」の行き先にならない。

    以前は画面の切り替えで出せるようにしていたが、出す用途が無いので
    **取り込みの段階で落として**、データそのものから消した。件数だけ残して見せる。
    """
    idx = spots.SpotIndex(nagareyama)
    assert all(s.get("dropin") is not False for s in idx.spots)
    cov = idx.coverage()
    assert cov["total"] == cov["dropin"]
    # 流山市 70 件（認可保育施設の CSV は出典ごと外したので、ここには入らない）
    assert cov["enrollment_excluded"] >= 70
    kinds = dict(cov["enrollment_types"])
    assert kinds["保育所"] > 0 and kinds["学童クラブ"] > 0 and kinds["幼稚園"] > 0


def test_facility_type_is_concrete(nagareyama):
    """「子育て施設」ではなく、保育所・児童館・子育て支援センターまで書く。"""
    idx = spots.SpotIndex(nagareyama)
    types = {s["facility_type"] for s in idx.spots}
    assert "子育て支援センター" in types
    assert "児童館・児童センター" in types
    assert "子育て施設" not in types
    assert "学童クラブ" not in types      # 入会が要るので取り込んでいない
    assert all(s["name"] for s in idx.spots)


def test_support_centers_have_verified_hours(nagareyama):
    """自前整備で、子育て支援センターの利用時間を出典つきで持っている。"""
    idx = spots.SpotIndex(nagareyama)
    centers = [s for s in idx.spots if s["facility_type"] == "子育て支援センター"]
    with_hours = [s for s in centers if s["open_time"]]
    assert len(with_hours) >= 15
    for s in with_hours:
        assert s["source_url"].startswith("https://www.city.nagareyama.chiba.jp/")
        assert s["checked_at"]


def test_events_are_dated(nagareyama):
    idx = spots.SpotIndex(nagareyama)
    events = [s for s in idx.spots if s["facility_type"] == "イベント"]
    assert events
    assert all(e["dropin"] for e in events)


def test_spots_have_summary_and_official_link(nagareyama):
    """S12 独自整備。**行き先を選べるだけの概要と、公式ページのリンク**を持っている。

    オープンデータの CSV には概要も URL も 1 件も入っていない。市の施設ページから
    自前で集めた `data/spots/nagareyama_details.csv` を重ねている。
    """
    idx = spots.SpotIndex(nagareyama)
    dropin = [s for s in idx.spots if s.get("dropin")]
    with_summary = [s for s in dropin if s.get("summary")]
    assert len(with_summary) >= 450
    assert len(with_summary) / len(dropin) > 0.85
    for s in with_summary:
        assert s["official_url"].startswith("http")
        assert s["summary"].endswith("。")


def test_playground_and_toilets_come_from_city_pages(nagareyama):
    """公園の遊具とトイレの有無は CSV に無い。市の施設ページから取っている。"""
    idx = spots.SpotIndex(nagareyama)
    parks = [s for s in idx.spots if s["facility_type"] == "公園"]
    assert len([p for p in parks if p.get("playground")]) >= 250
    assert len([p for p in parks if p.get("toilet")]) >= 20


def test_closed_facilities_are_not_shown(nagareyama):
    """**廃止された施設がオープンデータに残っている。**地図には出さず、件数で見せる。"""
    idx = spots.SpotIndex(nagareyama)
    names = {s["name"] for s in idx.spots}
    assert "子育て支援センターやぎきた" not in names
    assert "子育て支援センターKANADE" not in names
    assert idx.coverage()["closed"] == 3


def test_library_type_comes_from_city_page(nagareyama):
    """図書館はオープンデータでは「赤ちゃんほっとスペース」としか分からない。

    市のページの置き場所（文化施設 > 図書館）で、行き先として出せる種類に直している。
    """
    idx = spots.SpotIndex(nagareyama)
    libs = [s for s in idx.spots if s["facility_type"] == "図書館"]
    assert len(libs) == 3
    assert all(s["dropin"] for s in libs)
    assert all(s["age_max"] >= 155 for s in libs)
