"""S31 **行き先として出さない種類**（2026-09-30・ユーザー指示）。

「おむつ替えのできるトイレは、今日どこかに行きたいときの**行き先ではない**（着いた先で
使うもの）」ということで、画面から外した。**データは消していない。**
舞台の設定 `hide_types` に見出しの種類を並べると、`backend/spots.py` が索引を作るときに
落とす。`data/spots/generated/*.json` も取り込みのスクリプトもそのままなので、
設定の 2 行を消せば元どおりになる。

ここで確かめるのは 3 つ。
  1. 画面（API）から消えていること
  2. **データは残っている**こと
  3. **出どころで消していない**こと。ほこナビのトイレのデータには
     「公園の中のおむつ替えトイレ」が混ざっていて、取り込みのときに公園と統合されて
     `facility_type: 公園` になっているものがある。それは公園として残らなければならない
"""
import json

import pytest

from backend import profile as profile_mod, spots as spots_mod

HIDDEN = "おむつ替えのできるトイレ"
GENERATED = spots_mod.GENERATED


def raw_spots(stage_id):
    """**隠す前の、取り込んだままのデータ。**ファイルを直に読む。"""
    out = []
    for name in (stage_id, f"{stage_id}_outside"):
        path = GENERATED / f"{name}.json"
        if path.exists():
            out += json.loads(path.read_text(encoding="utf-8")).get("spots", [])
    return out


@pytest.mark.parametrize("stage_id", ["minato", "nagareyama"])
def test_both_stages_hide_the_same_kind(all_stages, stage_id):
    """**2 つの舞台でそろえる。** 港区だけ消えて流山市に残っていると筋が通らない。"""
    assert all_stages[stage_id].hide_types == [HIDDEN]


@pytest.mark.parametrize("stage_id", ["minato", "nagareyama"])
def test_the_data_is_still_there(stage_id):
    """**消したのではなく、出すのをやめただけ。** ファイルには残っている。"""
    assert [s for s in raw_spots(stage_id) if s.get("facility_type") == HIDDEN]


@pytest.mark.parametrize("stage_id", ["minato", "nagareyama"])
def test_the_index_does_not_serve_them(all_stages, stage_id):
    idx = spots_mod.SpotIndex(all_stages[stage_id])
    assert not [s for s in idx.spots + idx.outside if s.get("facility_type") == HIDDEN]
    assert idx.get(f"{HIDDEN}") is None
    # 落とした件数は理由として残す（画面の「この舞台で使えるデータ」に出す）
    assert dict(idx.coverage()["hidden_types"])[HIDDEN] == idx.coverage()["hidden_total"]


def test_parks_merged_from_the_toilet_data_survive(minato):
    """**出どころで消してはいけない。**

    港区のトイレのデータから来た 133 件のうち、40 件は同じ場所の公園と統合されて
    `facility_type: 公園` になっている（`id` は `tokyo_bf_toilet:` のまま）。
    出どころで落とすと、その公園まで地図から消える。
    """
    idx = spots_mod.SpotIndex(minato)
    from_toilet = [s for s in idx.spots if s["id"].startswith("tokyo_bf_toilet:")]
    assert len(from_toilet) >= 40
    assert {s["facility_type"] for s in from_toilet} == {"公園", "児童館・児童センター"}


def test_nagareyama_keeps_its_own_kinds(nagareyama):
    """**流山市で消すのは同じ見出しのものだけ**（ユーザー判断 2026-09-30）。

    市外の「授乳・おむつ替え」27 件と、市の登録制度である「赤ちゃんほっとスペース」50 件は
    行き先として残す。
    """
    idx = spots_mod.SpotIndex(nagareyama)
    kinds = {s.get("facility_type") for s in idx.spots + idx.outside}
    assert {"赤ちゃんほっとスペース", "授乳・おむつ替え"} <= kinds
    assert HIDDEN not in kinds


def test_outside_counts_are_recounted_after_hiding(minato):
    """**「市外の行き先 929 件」と言いながら 21 件しか出さない、をやらない。**

    市外の内訳は、作った時点の数字ではなく**残ったものから数え直す**。
    """
    meta = spots_mod.SpotIndex(minato).coverage()["outside"]
    assert meta["count"] == 21
    assert meta["hand"] == 21 and meta["from_dataset"] == 0
    assert sum(n for _, n in meta["by_city"]) == meta["count"]
    assert sum(n for _, n in meta["by_type"]) == meta["count"]
    assert "データセットから取ったもの" not in meta["source_note"]


def test_picks_are_unaffected_by_the_hidden_kind(minato):
    """左ペインの「近くの行き先」には元からトイレを出していない。そこは変わらない。"""
    idx = spots_mod.SpotIndex(minato)
    home = minato.homes[0]
    picks = idx.picks(home["lat"], home["lon"], profile_mod.for_mode("walk"), radius_km=2)
    assert picks
    assert not [s for s in picks if s.get("facility_type") == HIDDEN]
