"""When a load check asserts its timing, and when the burst runs (Q161, Q172, Q173)."""

from load.conftest import asserts_timing, selected_alone


def test_the_timing_is_asserted_under_m_load_on_the_laptop() -> None:
    assert asserts_timing("load", {})


def test_the_timing_is_not_asserted_in_ci_where_the_burst_still_runs() -> None:
    assert selected_alone("load")
    assert not asserts_timing("load", {"CI": "true"})


def test_the_default_suite_neither_asserts_the_timing_nor_runs_the_burst() -> None:
    assert not selected_alone("")
    assert not asserts_timing("", {})
    assert not asserts_timing("not posix", {})
