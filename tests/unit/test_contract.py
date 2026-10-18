import pytest

from money_trail.exceptions import DatasetConfigError
from money_trail.ingestion.contract import (
    AmountConverter, RecordError, TimestampParser, namespaced_id, parse_timezone, validate_namespace,
)

NS = "ds-one"


def code(fn, *a):
    with pytest.raises(RecordError) as e:
        fn(*a)
    return e.value.code


def test_namespace_validation():
    assert validate_namespace("a.b_c-1") == "a.b_c-1"
    for bad in ["", "has space", "co:lon", "-lead", "x" * 65, None]:
        with pytest.raises(DatasetConfigError):
            validate_namespace(bad)


def test_identifier_is_namespaced_and_not_double_prefixed():
    assert namespaced_id("acc1", NS, "f") == "ds-one:acc1"
    assert namespaced_id("ds-one:acc1", NS, "f") == "ds-one:acc1"
    assert namespaced_id(namespaced_id("acc1", NS, "f"), NS, "f") == "ds-one:acc1"   # idempotent
    assert namespaced_id("other:acc1", NS, "f") == "ds-one:other:acc1"               # foreign prefix is data
    assert namespaced_id("  x  ", NS, "f") == "ds-one:x"


@pytest.mark.parametrize("raw", [None, "", "   ", "ds-one:", "a\nb", "a\x00b", "x" * 300])
def test_invalid_identifiers(raw):
    assert code(namespaced_id, raw, NS, "f") in ("MISSING_VALUE", "INVALID_IDENTIFIER", "INVALID_VALUE")


CUR = {"USD": 2, "JPY": 0, "KWD": 3}


def test_amount_major_uses_per_currency_exponent():
    c = AmountConverter("major", CUR)
    assert c.to_minor("12.34", "USD") == 1234
    assert c.to_minor("12", "USD") == 1200
    assert c.to_minor("500", "JPY") == 500
    assert c.to_minor("1.234", "KWD") == 1234
    assert c.to_minor("0.1", "USD") == 10
    assert c.to_minor("0.10", "USD") == 10


def test_amount_is_exact_for_values_floats_get_wrong():
    c = AmountConverter("major", CUR)
    assert c.to_minor("0.07", "USD") == 7          # 0.07*100 == 7.000000000000001 in binary floats
    assert c.to_minor("1.15", "USD") == 115        # 1.15*100 == 114.99999999999999
    assert c.to_minor("4503599627370.99", "USD") == 450359962737099


@pytest.mark.parametrize("raw,cur", [("12.345", "USD"), ("1.5", "JPY"), ("-1", "USD"), ("1e3", "USD"),
                                     ("abc", "USD"), ("", "USD"), ("1,000.00", "USD"), ("NaN", "USD"),
                                     ("Infinity", "USD"), ("1.", "USD"), (".5", "USD"), ("99999999999999999999", "USD")])
def test_invalid_major_amounts_are_rejected_not_rounded(raw, cur):
    assert code(AmountConverter("major", CUR).to_minor, raw, cur) in ("INVALID_AMOUNT", "MISSING_VALUE")


def test_amount_minor_unit_and_int64_bound():
    c = AmountConverter("minor", CUR)
    assert c.to_minor("1234", "USD") == 1234
    assert code(c.to_minor, "12.5", "USD") == "INVALID_AMOUNT"
    assert code(c.to_minor, "9223372036854775808", "USD") == "INVALID_AMOUNT"
    assert c.to_minor("9223372036854775807", "USD") == 2**63 - 1


def test_currency_validation():
    c = AmountConverter("major", CUR)
    assert c.currency(" usd ") == "USD"
    for bad in ["XXX", "US", "USDD", "12A", "", None]:
        assert code(c.currency, bad) in ("UNSUPPORTED_CURRENCY", "MISSING_VALUE")


def test_timestamp_iso_with_offset_and_z():
    p = TimestampParser("iso8601")
    assert p.to_epoch_ms("1970-01-01T00:00:00Z") == 0
    assert p.to_epoch_ms("2026-01-01T00:00:00.123Z") == 1767225600123
    assert p.to_epoch_ms("2026-01-01T05:30:00+05:30") == 1767225600000
    assert p.to_epoch_ms("2026-01-01T00:00:00.123+00:00") == 1767225600123


def test_naive_timestamp_without_configured_timezone_is_ambiguous():
    assert code(TimestampParser("iso8601").to_epoch_ms, "2026-01-01T00:00:00") == "AMBIGUOUS_TIMESTAMP"
    assert code(TimestampParser("strptime", "%d/%m/%Y %H:%M").to_epoch_ms, "01/02/2026 10:00") == "AMBIGUOUS_TIMESTAMP"


def test_naive_timestamp_with_configured_timezone():
    p = TimestampParser("iso8601", tz=parse_timezone("+05:30"))
    assert p.to_epoch_ms("2026-01-01T05:30:00") == 1767225600000
    assert TimestampParser("strptime", "%d/%m/%Y %H:%M", parse_timezone("UTC")).to_epoch_ms("01/01/2026 00:00") == 1767225600000


def test_dst_ambiguous_local_time_rejected_and_unambiguous_accepted():
    tz = parse_timezone("America/New_York")
    p = TimestampParser("iso8601", tz=tz)
    assert code(p.to_epoch_ms, "2026-11-01T01:30:00") == "AMBIGUOUS_TIMESTAMP"   # repeated hour
    assert code(p.to_epoch_ms, "2026-03-08T02:30:00") == "AMBIGUOUS_TIMESTAMP"   # skipped hour
    assert p.to_epoch_ms("2026-01-01T00:00:00") == 1767243600000                 # UTC-5


def test_epoch_formats_and_bad_values():
    assert TimestampParser("epoch_ms").to_epoch_ms("1767225600123") == 1767225600123
    assert TimestampParser("epoch_s").to_epoch_ms("1767225600") == 1767225600000
    for fmt in ("epoch_ms", "epoch_s"):
        for bad in ["12.5", "-1", "abc", ""]:
            assert code(TimestampParser(fmt).to_epoch_ms, bad) in ("INVALID_TIMESTAMP", "MISSING_VALUE")


@pytest.mark.parametrize("bad", ["yesterday", "2026-13-01T00:00:00Z", "2026-02-30T00:00:00Z", "2026-01-01"[:5]])
def test_malformed_timestamps(bad):
    assert code(TimestampParser("iso8601").to_epoch_ms, bad) == "INVALID_TIMESTAMP"


def test_sub_millisecond_precision_is_rejected_not_truncated():
    assert code(TimestampParser("iso8601").to_epoch_ms, "2026-01-01T00:00:00.123456Z") == "INVALID_TIMESTAMP"


def test_negative_before_epoch_rejected():
    assert code(TimestampParser("iso8601").to_epoch_ms, "1969-12-31T23:59:59Z") == "INVALID_TIMESTAMP"


def test_bad_timezone_config():
    for bad in ["Mars/Olympus", "+5", "EST5EDT5"]:
        with pytest.raises(DatasetConfigError):
            parse_timezone(bad)
