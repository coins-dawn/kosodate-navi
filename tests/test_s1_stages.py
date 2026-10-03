"""S1 舞台の切り替えと境界。"""


def test_usable_stages(all_stages):
    """**使える舞台は流山市と港区。** 港区は 2026-09-27 に足した（横展開先の選定の結果）。"""
    assert set(all_stages) == {"nagareyama", "minato"}


def test_disabled_stage_is_listed_but_carries_no_data(every_stage):
    """**まだ使えない舞台**は、画面にボタンを出すための見出しだけを持つ。

    新宿区は 2026-09-25 にデータごと取り下げた。後から足す場所が画面に見えるよう
    `data/stages/shinjuku.yaml` に名前だけ残してあり、`enabled: false` なので
    データ整備のスクリプト（load_all()）は触らない。
    """
    assert set(every_stage) == {"nagareyama", "minato", "shinjuku"}
    stub = every_stage["shinjuku"]
    assert stub.enabled is False
    assert stub.note                       # 押せない理由を画面に出す
    assert stub.boundary() is None         # 区域・時刻表・スポットは 1 件も無い
    assert stub.transit == [] and stub.spots == []
    assert not stub.db_path.exists()


def test_area_matches_reality(nagareyama):
    # 国土数値情報 N03 から切り出した面積（計画の完了条件）
    assert round(nagareyama.area_km2(), 1) == 35.2


def test_home_must_be_inside(nagareyama):
    assert nagareyama.contains(35.8562, 139.9029)      # 市役所あたり
    assert not nagareyama.contains(35.6938, 139.7036)  # 新宿区役所あたり


def test_stage_without_indoor_is_fine(nagareyama):
    """**持っていない層は「なし」でよい。** 流山市は駅の中もほこナビも無い。"""
    assert nagareyama.indoor is None
    assert nagareyama.coverage()["indoor"] is False
    assert nagareyama.coverage()["hokonavi"] is False
