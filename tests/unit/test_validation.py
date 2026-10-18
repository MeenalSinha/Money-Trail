import csv
import json
from pathlib import Path

import pytest

from money_trail.ingestion.config import parse_dataset_config
from money_trail.ingestion.validation import QuarantineWriter, iter_accepted, validate_dataset

ACC_H = ["id", "opened"]
TX_H = ["tid", "src", "dst", "amt", "cur", "ts", "chan"]
AL_H = ["aid", "tid", "at", "kind"]
TS = "2026-01-01T00:00:00Z"


def build(tmp_path, accounts=None, txs=None, alerts=None, delimiter=",", encoding="utf-8", **ing):
    def w(name, header, rows):
        with open(tmp_path / name, "w", newline="", encoding=encoding) as f:
            wr = csv.writer(f, delimiter=delimiter, lineterminator="\n")
            wr.writerow(header)
            wr.writerows(rows)
    files = {"transactions": {"path": "t.csv", "columns": {
        "transaction_id": "tid", "source_account": "src", "destination_account": "dst", "amount": "amt",
        "currency": "cur", "timestamp": "ts", "channel": "chan"}}}
    w("t.csv", TX_H, txs or [])
    ingestion = {"batch_size": 2, "invalid_policy": "strict", "source_conflict_policy": "reject",
                 "graph_conflict_policy": "reject", "accounts_policy": "require_file", "max_reported_errors": 20}
    if accounts is not None:
        w("a.csv", ACC_H, accounts)
        files["accounts"] = {"path": "a.csv", "columns": {"account_id": "id", "created_at": "opened"}}
    else:
        ingestion["accounts_policy"] = "derive_from_transactions"
    if alerts is not None:
        w("al.csv", AL_H, alerts)
        files["alerts"] = {"path": "al.csv", "columns": {"alert_id": "aid", "transaction_id": "tid",
                                                          "created_at": "at", "alert_type": "kind"}}
    ingestion.update(ing)
    raw = {"dataset": {"namespace": "ns", "source": {"name": "unit"}}, "format": "csv",
           "csv": {"delimiter": delimiter, "encoding": encoding}, "files": files,
           "normalization": {"timestamp": {"format": "iso8601"},
                             "amount": {"unit": "major", "currencies": {"USD": 2, "JPY": 0}}},
           "ingestion": ingestion, "output": {"quarantine_dir": "q", "report_dir": "r"}}
    return parse_dataset_config(raw, tmp_path / "cfg.json")


A = [["a1", TS], ["a2", TS], ["a3", ""]]
T = [["t1", "a1", "a2", "10.00", "USD", TS, "x"], ["t2", "a2", "a3", "5", "JPY", TS, ""]]


def codes(out):
    return {(e, c): n for (e, c), n in out.error_counts.items()}


def test_valid_dataset_is_clean(tmp_path):
    out = validate_dataset(build(tmp_path, A, T, [["al1", "t1", TS, "k"]]))
    assert out.error_total == 0 and out.fatal is None
    assert [out.stats[e].accepted for e in ("accounts", "transactions", "alerts")] == [3, 2, 1]


def test_source_flexible_mapping_delimiter_and_encoding(tmp_path):
    cfg = build(tmp_path, A, T, delimiter=";", encoding="latin-1")
    assert validate_dataset(cfg).error_total == 0


def test_missing_required_column_is_fatal_with_actionable_message(tmp_path):
    cfg = build(tmp_path, A, T)
    p = tmp_path / "t.csv"
    p.write_text(p.read_text().replace("amt", "amount_renamed", 1))
    out = validate_dataset(cfg)
    assert out.fatal and "amt" in out.fatal and "missing mapped source columns" in out.fatal


def test_missing_file_and_empty_file_are_fatal(tmp_path):
    cfg = build(tmp_path, A, T)
    (tmp_path / "t.csv").write_text("")
    assert "empty" in validate_dataset(cfg).fatal
    (tmp_path / "t.csv").unlink()
    assert "not found" in validate_dataset(cfg).fatal


@pytest.mark.parametrize("row,code", [
    (["t1", "a1", "a2", "-1", "USD", TS, ""], "INVALID_AMOUNT"),
    (["t1", "a1", "a2", "1.234", "USD", TS, ""], "INVALID_AMOUNT"),
    (["t1", "a1", "a2", "abc", "USD", TS, ""], "INVALID_AMOUNT"),
    (["t1", "a1", "a2", "1", "XXX", TS, ""], "UNSUPPORTED_CURRENCY"),
    (["t1", "a1", "a2", "1", "", TS, ""], "MISSING_VALUE"),
    (["t1", "a1", "a2", "1", "USD", "tomorrow", ""], "INVALID_TIMESTAMP"),
    (["t1", "a1", "a2", "1", "USD", "2026-01-01T00:00:00", ""], "AMBIGUOUS_TIMESTAMP"),
    (["", "a1", "a2", "1", "USD", TS, ""], "MISSING_VALUE"),
    (["t1", "", "a2", "1", "USD", TS, ""], "MISSING_VALUE"),
    (["t1", "a1", "a2", "1", "USD", TS], "MALFORMED_ROW"),
    (["t1", "a1", "a2", "1", "USD", TS, "x", "extra"], "MALFORMED_ROW"),
    (["t1", "a1", "zz", "1", "USD", TS, ""], "UNKNOWN_ACCOUNT"),
    (["t1", "zz", "a2", "1", "USD", TS, ""], "UNKNOWN_ACCOUNT"),
])
def test_invalid_transaction_rows_are_rejected_with_codes(tmp_path, row, code):
    out = validate_dataset(build(tmp_path, A, [row]))
    assert codes(out) == {("transactions", code): 1}
    assert out.stats["transactions"].accepted == 0 and out.skip["transactions"] == {1}
    s = out.error_samples[0]
    assert s["record_number"] == 1 and s["line"] >= 2 and "value" not in s


def test_error_messages_never_contain_source_values(tmp_path):
    out = validate_dataset(build(tmp_path, A, [["SECRETID", "a1", "a2", "SECRETAMT", "USD", TS, ""]]))
    assert "SECRETAMT" not in json.dumps(out.error_samples) and "SECRETID" not in json.dumps(out.error_samples)


def test_malformed_csv_quoting(tmp_path):
    cfg = build(tmp_path, A, T)
    (tmp_path / "t.csv").write_text("tid,src,dst,amt,cur,ts,chan\n"
                                    't1,a1,a2,1,USD,2026-01-01T00:00:00Z,"bad"quote\n'
                                    "t2,a2,a3,5,JPY,2026-01-01T00:00:00Z,\n")
    out = validate_dataset(cfg)
    assert codes(out) == {("transactions", "MALFORMED_ROW"): 1} and out.stats["transactions"].accepted == 1


def test_blank_lines_are_ignored_and_ids_not_double_prefixed(tmp_path):
    cfg = build(tmp_path, [["ns:a1", TS], ["a2", TS]], [["t1", "ns:a1", "a2", "1", "USD", TS, ""]])
    (tmp_path / "t.csv").write_text((tmp_path / "t.csv").read_text() + "\n\n")
    out = validate_dataset(cfg)
    assert out.error_total == 0
    row = next(iter_accepted(cfg, cfg.transactions, out))
    assert row.extra["source_account"] == "ns:a1" and row.record["transaction_id"] == "ns:t1"


def test_identical_duplicates_are_skipped_not_errors(tmp_path):
    out = validate_dataset(build(tmp_path, A + [["a1", TS]], T + [T[0]]))
    assert out.error_total == 0
    assert out.stats["accounts"].duplicates_identical == 1 and out.stats["transactions"].duplicates_identical == 1
    assert out.stats["transactions"].accepted == 2


def test_conflicting_duplicates_reject_policy(tmp_path):
    dup = ["t1", "a1", "a2", "99.00", "USD", TS, "x"]
    out = validate_dataset(build(tmp_path, A, T + [dup]))
    assert codes(out) == {("transactions", "DUPLICATE_CONFLICT"): 1}
    assert out.stats["transactions"].accepted == 2 and out.stats["transactions"].rejected == 1


def test_conflicting_duplicates_keep_first_policy(tmp_path):
    dup = ["t1", "a1", "a2", "99.00", "USD", TS, "x"]
    out = validate_dataset(build(tmp_path, A, T + [dup], source_conflict_policy="keep_first"))
    assert out.error_total == 0 and out.stats["transactions"].source_conflicts_skipped == 1
    cfg = build(tmp_path, A, T + [dup], source_conflict_policy="keep_first")
    kept = [r.record["amount"] for r in iter_accepted(cfg, cfg.transactions, out)]
    assert kept == [1000, 5]


def test_rejected_account_makes_dependent_transactions_unknown(tmp_path):
    out = validate_dataset(build(tmp_path, [["a1", TS], ["", TS], ["a3", TS]], T))
    assert codes(out) == {("accounts", "MISSING_VALUE"): 1, ("transactions", "UNKNOWN_ACCOUNT"): 2}


def test_alert_reference_validation(tmp_path):
    out = validate_dataset(build(tmp_path, A, T, [["al1", "t1", TS, "k"], ["al2", "nope", TS, "k"],
                                                  ["al3", "t1", "bad", "k"]]))
    assert codes(out) == {("alerts", "UNKNOWN_TRANSACTION"): 1, ("alerts", "INVALID_TIMESTAMP"): 1}
    assert out.stats["alerts"].accepted == 1


def test_alert_pointing_to_rejected_transaction_is_rejected(tmp_path):
    bad = ["t9", "a1", "a2", "oops", "USD", TS, ""]
    out = validate_dataset(build(tmp_path, A, T + [bad], [["al1", "t9", TS, "k"]]))
    assert ("alerts", "UNKNOWN_TRANSACTION") in codes(out)


def test_derive_accounts_is_explicit_and_counted(tmp_path):
    out = validate_dataset(build(tmp_path, None, T))
    assert out.derived_accounts == {"ns:a1", "ns:a2", "ns:a3"} and out.error_total == 0
    assert out.stats["accounts"].accepted == 3


def test_record_limits(tmp_path):
    out = validate_dataset(build(tmp_path, A, T, max_records_per_file=1))
    assert "max_records_per_file" in out.fatal
    out = validate_dataset(build(tmp_path, A, T, max_file_bytes=10))
    assert "max_file_bytes" in out.fatal


def test_quarantine_writer_keeps_raw_row_and_reasons(tmp_path):
    cfg = build(tmp_path, A, [["t1", "a1", "a2", "bad", "USD", TS, ""], T[1]])
    q = QuarantineWriter(tmp_path / "quar")
    out = validate_dataset(cfg, q)
    q.close()
    lines = [json.loads(l) for l in (tmp_path / "quar" / "transactions.jsonl").read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["record_number"] == 1
    assert lines[0]["errors"][0]["code"] == "INVALID_AMOUNT" and lines[0]["raw"]["tid"] == "t1"
    assert out.stats["transactions"].accepted == 1


def test_error_samples_are_capped_but_counts_are_exact(tmp_path):
    rows = [[f"t{i}", "a1", "a2", "bad", "USD", TS, ""] for i in range(30)]
    out = validate_dataset(build(tmp_path, A, rows, max_reported_errors=3))
    assert out.error_total == 30 and len(out.error_samples) == 3
