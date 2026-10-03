"""時刻表データの更新チェック（開発者ガイドライン 2.2.2）。

ガイドライン 2.2.2 は「静的データを表示するなら定期的に最新を取得して更新する」
「センターが更新を通知してから **1 週間以内**に更新する」ことを求めている。
このサービスは**結果を先に計算して配る**ので、元データが変わっても自動では追随しない。

`scripts/check_data_updates.py` の判定だけは、**ネットワークなしで確かめられるように**
切り出してある。配信側は中身が同じでもファイルを置き直すことがあるので、
「日付が新しい＝更新」ではない。
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
spec = importlib.util.spec_from_file_location("check_data_updates",
                                              REPO / "scripts" / "check_data_updates.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_size_difference_means_updated():
    assert checker.verdict(100, None, 120, True) == "更新あり"
    assert checker.verdict(100, None, 120, False) == "更新あり"   # 日付が古くても中身が違う


def test_same_size_and_not_newer_is_up_to_date():
    assert checker.verdict(100, None, 100, False) == "最新"


def test_same_size_but_newer_needs_the_body():
    """**日付だけでは判断しない。** 中身を突き合わせるまでは「要確認」。"""
    assert checker.verdict(100, None, 100, True) == "要確認"
    assert checker.verdict(100, None, 100, True, same_body=True) == "最新（配信し直しただけ）"
    assert checker.verdict(100, None, 100, True, same_body=False) == "更新あり"


def test_missing_local_file():
    assert checker.verdict(None, None, 100, True) == "手元に無い"
