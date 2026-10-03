"""S9 天気・暑さ。取れなくても壊れないこと。"""
from backend import weather


def test_no_forecast_means_no_advice():
    assert weather.advice({"indoor": False}, None) == []


def test_outdoor_gets_rain_warning():
    fc = {"weather": "雨", "pops": ["60"], "temp_max": 24}
    notes = weather.advice({"indoor": False}, fc)
    assert any(n["level"] == "warn" for n in notes)


def test_indoor_is_offered_as_an_option():
    fc = {"weather": "雨", "pops": ["60"], "temp_max": 24}
    notes = weather.advice({"indoor": True}, fc)
    assert notes and notes[0]["level"] == "info"


def test_hot_day_mentions_stroller():
    fc = {"weather": "晴", "pops": ["0"], "temp_max": 36}
    notes = weather.advice({"indoor": False}, fc)
    assert any("ベビーカー" in n["text"] for n in notes)
