from scenarios import months_earlier


def test_dates_move_two_months_back_in_iso_and_chinese_text():
    assert months_earlier("2026-12-03T08:00:00+08:00") == "2026-10-03T08:00:00+08:00"
    assert months_earlier("2026-01-05") == "2025-11-05"
    assert months_earlier("北岸门诊10月26日夜班，见2026年9月报告") == "北岸门诊8月26日夜班，见2026年7月报告"
