from src.services.scheduler import should_run_now


def test_should_run_now_at_configured_time():
    assert should_run_now("09:00", 9, 0) is True
    assert should_run_now("09:00", 9, 1) is False
    assert should_run_now("9:05", 9, 5) is True
