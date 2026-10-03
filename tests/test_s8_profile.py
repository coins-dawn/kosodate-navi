"""S8 年齢プロファイル。"""
from backend import profile


def test_bands_cover_the_slider():
    for months in range(-9, 157):
        assert profile.for_months(months) is not None


def test_tiers_are_three():
    tiers = {b.get("tier") for b in profile.load_bands()}
    assert tiers == {"stroller", "gentle", "normal"}


def test_stroller_can_be_overridden():
    assert profile.for_months(30).stroller is True
    assert profile.for_months(30, stroller=False).stroller is False


def test_spot_age_filter():
    p = profile.for_months(96)
    assert p.spot_matches({"age_min": 72, "age_max": 155})
    assert not p.spot_matches({"age_min": -9, "age_max": 35})


def test_relaxed_allows_steps():
    p = profile.for_months(6)
    assert p.band["steps"] is None
    assert p.relaxed().band["steps"] == 6.0
