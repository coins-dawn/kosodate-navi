"""S5 ほこナビの重ね合わせ。

**いまの舞台（流山市）にほこナビのデータは無い。** 歩行空間ネットワークが整備されて
いるのは都市部の一部だけで、それ自体がこのサービスの見せたいこと（データの偏り）なので、
**無いときに何が起きるか**を確かめる。重ねる側の処理は残してあるので、
ほこナビのある舞台を足せばそのまま効く（`walk_layers` に `kind: hokonavi` を書く）。
"""
from backend import barrier


def test_nagareyama_has_none(nagareyama):
    cov = barrier.BarrierLayer(nagareyama).coverage()
    assert cov["available"] is False
    assert cov["km"] == 0.0


def test_missing_layer_is_said_out_loud(nagareyama):
    layer = barrier.BarrierLayer(nagareyama)
    path = {"coords": [[139.9029, 35.8562], [139.9035, 35.8570]], "warnings": []}
    layer.annotate(path, None)
    assert any("段差データがありません" in w for w in path["warnings"])


def test_lookup_is_none_without_data(nagareyama):
    assert barrier.BarrierLayer(nagareyama).lookup(35.8562, 139.9029) is None
