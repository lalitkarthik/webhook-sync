"""Encoding's pure rules as a table test (spec "Encoding"; sheets-sink.md "Encoding values").

The flusher tests drive the same rules through the seam; these pin each rule's edge cases.
"""

from typing import Any

import pytest

from webhook_sync.sinks.encoding import (
    CELL_LIMIT,
    TRUNCATED,
    UnknownCurrency,
    amount,
    exponent,
    mask_email,
    note,
    text,
)


def utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


@pytest.mark.parametrize(
    ("currency", "expected"),
    [
        ("INR", 2),
        ("inr", 2),  # codes upper-cased
        ("USD", 2),
        ("JPY", 0),
        ("KRW", 0),
        ("MGA", 0),  # Stripe zero-decimal, two in ISO 4217
        ("HUF", 2),
        ("TWD", 2),
        ("IQD", 3),
    ],
)
def test_the_exponent_comes_from_iso_4217_and_stripes_lists(currency: str, expected: int) -> None:
    assert exponent(currency) == expected


@pytest.mark.parametrize("currency", ["XYZ", "", "BHD", "JOD", "KWD", "OMR", "TND", "UGX", "ISK"])
def test_an_unknown_code_and_the_seven_held_back_codes_are_unknown(currency: str) -> None:
    with pytest.raises(UnknownCurrency):
        exponent(currency)


@pytest.mark.parametrize(
    ("minor", "currency", "value", "pattern"),
    [
        (1999, "JPY", 1999.0, "0"),
        (1999, "USD", 19.99, "0.00"),
        (1999, "IQD", 1.999, "0.000"),
        (50000, "INR", 500.0, "0.00"),
        (-1999, "USD", -19.99, "0.00"),
        (123456789012345, "INR", 1234567890123.45, "0.00"),  # 15 digits: still a number
        (10**20, "INR", 1e18, "0.00"),  # one significant digit: exact as a double
    ],
)
def test_an_amount_is_a_number_with_a_format_per_exponent(
    minor: int, currency: str, value: float, pattern: str
) -> None:
    assert amount(minor, currency) == {
        "userEnteredValue": {"numberValue": value},
        "userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}},
    }


def test_an_amount_past_15_significant_digits_is_text() -> None:
    assert amount(1234567890123456, "INR") == {
        "userEnteredValue": {"stringValue": "12345678901234.56"}
    }
    assert amount(1234567890123456, "JPY") == {
        "userEnteredValue": {"stringValue": "1234567890123456"}
    }


def test_no_amount_is_an_empty_cell_whatever_the_currency() -> None:
    assert amount(None, "INR") == {}


@pytest.mark.parametrize("value", ['=HYPERLINK("x")', "+919876543210", "007", "1e5", "TRUE"])
def test_text_is_a_string_value_as_given(value: str) -> None:
    assert text(value) == {"userEnteredValue": {"stringValue": value}}


def test_a_cell_over_the_limit_is_cut_ending_truncated() -> None:
    cut: Any = text("a" * 60000)["userEnteredValue"]["stringValue"]
    assert utf16_units(cut) == CELL_LIMIT == 50000
    assert cut.endswith(TRUNCATED)
    assert TRUNCATED == "…[truncated]"


def test_the_cut_counts_utf16_units_and_never_splits_a_surrogate_pair() -> None:
    keep = CELL_LIMIT - utf16_units(TRUNCATED)
    # An emoji (two units) straddles the cut point: it is dropped whole.
    value = "a" * (keep - 1) + "😀" * 10
    cut: Any = text(value)["userEnteredValue"]["stringValue"]
    assert cut == "a" * (keep - 1) + TRUNCATED
    cut.encode("utf-8")  # no lone surrogate
    # An emoji that fits whole is kept.
    value = "a" * (keep - 2) + "😀" * 10
    cut = text(value)["userEnteredValue"]["stringValue"]
    assert cut == "a" * (keep - 2) + "😀" + TRUNCATED
    assert utf16_units(cut) == CELL_LIMIT


def test_a_cell_at_the_limit_is_kept_whole() -> None:
    value = "😀" * (CELL_LIMIT // 2)
    assert text(value) == {"userEnteredValue": {"stringValue": value}}


@pytest.mark.parametrize(
    ("email", "masked"),
    [
        ("a.b@gmail.com", "a***@gmail.com"),
        ("x", "***"),
        ("@gmail.com", "***@gmail.com"),
        ("ab@c@d.com", "a***@d.com"),
    ],
)
def test_an_email_is_masked_to_its_first_character_and_domain(email: str, masked: str) -> None:
    assert mask_email(email) == masked


@pytest.mark.parametrize(
    ("not_found_at", "unknown_columns", "expected"),
    [
        (None, None, ""),
        ("2026-10-02T16:00:00.000000Z", None, "not found at source"),
        (None, '["fee", "gst_on_fee"]', "unknown: fee, gst_on_fee"),
        (None, "fee", "unknown: fee"),
        (
            "2026-10-02T16:00:00.000000Z",
            '["fee"]',
            "not found at source; unknown: fee",
        ),
    ],
)
def test_the_note_explains_odd_rows(
    not_found_at: str | None, unknown_columns: str | None, expected: str
) -> None:
    assert note({"not_found_at": not_found_at, "unknown_columns": unknown_columns}) == expected
