import copy
import json

import pytest

from money_trail.exceptions import DatasetConfigError
from money_trail.ingestion.config import load_dataset_config, parse_dataset_config
from pathlib import Path

BASE = {
    "dataset": {"namespace": "ns1", "source": {"name": "unit"}},
    "format": "csv", "csv": {"delimiter": ",", "encoding": "utf-8"},
    "files": {
        "accounts": {"path": "a.csv", "columns": {"account_id": "id"}},
        "transactions": {"path": "t.csv", "columns": {
            "transaction_id": "tid", "source_account": "s", "destination_account": "d",
            "amount": "amt", "currency": "cur", "timestamp": "ts"}},
    },
    "normalization": {"timestamp": {"format": "iso8601"},
                      "amount": {"unit": "major", "currencies": {"USD": 2}}},
    "ingestion": {"batch_size": 10, "invalid_policy": "strict", "source_conflict_policy": "reject",
                  "graph_conflict_policy": "reject", "accounts_policy": "require_file", "max_reported_errors": 5},
    "output": {"quarantine_dir": "q", "report_dir": "r"},
}


def parse(mutate=None):
    raw = copy.deepcopy(BASE)
    if mutate:
        mutate(raw)
    return parse_dataset_config(raw, Path("/cfg/ingest.json"))


def test_valid_config_resolves_relative_paths():
    c = parse()
    assert c.namespace == "ns1" and c.transactions.path == Path("/cfg/t.csv")
    assert c.report_dir == Path("/cfg/r") and c.alerts is None and c.batch_size == 10


def rm(path):
    def f(raw):
        d = raw
        for k in path[:-1]:
            d = d[k]
        del d[path[-1]]
    return f


@pytest.mark.parametrize("path", [
    ("dataset",), ("format",), ("csv", "delimiter"), ("normalization", "timestamp"),
    ("normalization", "amount", "unit"), ("ingestion", "batch_size"), ("ingestion", "invalid_policy"),
    ("ingestion", "source_conflict_policy"), ("ingestion", "graph_conflict_policy"),
    ("ingestion", "accounts_policy"), ("output", "report_dir"), ("output", "quarantine_dir"),
    ("files", "transactions"), ("dataset", "namespace"),
])
def test_every_operational_setting_is_required(path):
    with pytest.raises(DatasetConfigError):
        parse(rm(path))


def test_required_column_mappings():
    with pytest.raises(DatasetConfigError, match="must map"):
        parse(rm(("files", "transactions", "columns", "amount")))
    with pytest.raises(DatasetConfigError, match="must map"):
        parse(rm(("files", "transactions", "columns", "currency")))


def test_fixed_currency_replaces_currency_column():
    def m(raw):
        del raw["files"]["transactions"]["columns"]["currency"]
        raw["normalization"]["amount"]["fixed_currency"] = "USD"
    assert parse(m).fixed_currency == "USD"

    def both(raw):
        raw["normalization"]["amount"]["fixed_currency"] = "USD"
    with pytest.raises(DatasetConfigError, match="choose one"):
        parse(both)


@pytest.mark.parametrize("mutate,match", [
    (lambda r: r["ingestion"].update(invalid_policy="lenient"), "invalid_policy"),
    (lambda r: r["ingestion"].update(graph_conflict_policy="overwrite"), "graph_conflict_policy"),
    (lambda r: r["ingestion"].update(source_conflict_policy="last_wins"), "source_conflict_policy"),
    (lambda r: r["ingestion"].update(batch_size=0), "batch_size"),
    (lambda r: r["ingestion"].update(batch_size=10**6), "batch_size"),
    (lambda r: r["ingestion"].update(max_records_per_file=-1), "max_records_per_file"),
    (lambda r: r["csv"].update(delimiter=",,"), "delimiter"),
    (lambda r: r["csv"].update(encoding="not-a-codec"), "encoding"),
    (lambda r: r["dataset"].update(namespace="bad:ns"), "namespace"),
    (lambda r: r.update(format="json"), "format"),
    (lambda r: r.update(surprise=1), "unknown keys"),
    (lambda r: r["files"]["accounts"]["columns"].update(extra_field="x"), "unknown keys"),
    (lambda r: r["files"]["accounts"]["columns"].update(created_at="id"), "several canonical"),
    (lambda r: r["normalization"]["amount"]["currencies"].update(usd=2), "currencies"),
    (lambda r: r["normalization"]["amount"]["currencies"].update(EUR=9), "currencies"),
    (lambda r: r["normalization"]["timestamp"].update(format="strptime"), "pattern"),
    (lambda r: r["normalization"]["timestamp"].update(timezone="Mars/X"), "timezone"),
    (lambda r: r["normalization"]["amount"].update(fixed_currency="EUR"), "fixed_currency"),
])
def test_invalid_values_rejected(mutate, match):
    with pytest.raises(DatasetConfigError, match=match):
        parse(mutate)


def test_accounts_policy_consistency():
    with pytest.raises(DatasetConfigError, match="require_file"):
        parse(lambda r: r["files"].pop("accounts"))
    with pytest.raises(DatasetConfigError, match="derive_from_transactions"):
        parse(lambda r: r["ingestion"].update(accounts_policy="derive_from_transactions"))

    def derive(r):
        r["files"].pop("accounts")
        r["ingestion"]["accounts_policy"] = "derive_from_transactions"
    assert parse(derive).accounts is None


def test_load_errors(tmp_path):
    with pytest.raises(DatasetConfigError, match="not found"):
        load_dataset_config(tmp_path / "x.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(DatasetConfigError, match="not valid JSON"):
        load_dataset_config(bad)
    arr = tmp_path / "arr.json"
    arr.write_text("[]")
    with pytest.raises(DatasetConfigError, match="object"):
        load_dataset_config(arr)


def test_fingerprint_changes_with_content():
    a, b = parse(), parse(lambda r: r["ingestion"].update(batch_size=11))
    assert a.fingerprint != b.fingerprint and a.fingerprint == parse().fingerprint


def test_shipped_example_configs_are_valid():
    root = Path(__file__).resolve().parents[2] / "examples"
    ex = load_dataset_config(root / "ingest.example.json")
    assert ex.namespace == "example-bank-export" and ex.fixed_currency == "INR" and ex.timestamp.fmt == "strptime"
    from money_trail.generation import load_generator_config
    g = load_generator_config(root / "generator.example.json")
    assert g.namespace == "synthetic-demo" and len(g.patterns) == 4
