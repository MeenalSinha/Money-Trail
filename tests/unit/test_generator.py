import copy
import json
from collections import Counter

import pytest

from support import generator_dict
from money_trail.exceptions import DatasetConfigError
from money_trail.generation import generate, load_generator_config, write_dataset
from money_trail.generation.config import parse_generator_config
from money_trail.ingestion import load_dataset_config, validate_dataset
from pathlib import Path


def snapshot(d):
    return {n: (d / n).read_bytes() for n in ("accounts.csv", "transactions.csv", "alerts.csv", "ground_truth.json")}


def test_same_config_and_seed_give_identical_data(make_dataset):
    a, b = make_dataset("ns-a", seed=7), make_dataset("ns-a", seed=7)
    assert snapshot(a.dir) == snapshot(b.dir)


def test_different_seed_gives_different_data(make_dataset):
    assert snapshot(make_dataset("ns-a", seed=7).dir) != snapshot(make_dataset("ns-a", seed=8).dir)


def test_generated_records_satisfy_the_canonical_contract(make_dataset):
    ds = make_dataset()
    out = validate_dataset(load_dataset_config(ds.ingest_config))
    assert out.error_total == 0 and out.fatal is None
    counts = ds.manifest["record_counts"]
    assert [out.stats[e].accepted for e in ("accounts", "transactions", "alerts")] == \
           [counts["accounts"], counts["transactions"], counts["alerts"]]


def test_every_transaction_references_generated_accounts(make_dataset):
    ds = make_dataset()
    accounts = {r["account_id"] for r in ds.rows("accounts.csv")}
    for t in ds.rows("transactions.csv"):
        assert t["from_account"] in accounts and t["to_account"] in accounts
    ids = [t["txn_id"] for t in ds.rows("transactions.csv")]
    assert len(ids) == len(set(ids))


def test_transactions_are_inside_the_configured_window(make_dataset):
    from money_trail.ingestion.contract import TimestampParser
    ds, p = make_dataset(), TimestampParser("iso8601")
    lo, hi = ds.gen_cfg.window_start_ms, ds.gen_cfg.window_start_ms + ds.gen_cfg.window_ms
    for t in ds.rows("transactions.csv"):
        assert lo <= p.to_epoch_ms(t["booked_at"]) < hi


def _gt(ds):
    return json.loads((ds.dir / "ground_truth.json").read_text())


def _tx_index(ds):
    from money_trail.ingestion.contract import TimestampParser, namespaced_id
    ns, p = ds.gen_cfg.namespace, TimestampParser("iso8601")
    out = {}
    for t in ds.rows("transactions.csv"):
        out[namespaced_id(t["txn_id"], ns, "x")] = (namespaced_id(t["from_account"], ns, "x"),
                                                  namespaced_id(t["to_account"], ns, "x"),
                                                  p.to_epoch_ms(t["booked_at"]), t["amount"])
    return out


def test_ground_truth_matches_the_generated_patterns(make_dataset):
    ds = make_dataset()
    gt, tx = _gt(ds), _tx_index(ds)
    assert Counter(p["type"] for p in gt["patterns"]) == {"circular": 1, "pass_through": 1, "fan_in": 1, "fan_out": 1}
    for p in gt["patterns"]:
        members = [tx[t] for t in p["transaction_ids"]]       # every labelled tx exists in the data
        assert {a for m in members for a in m[:2]} == set(p["account_ids"])
        stamps = [m[2] for m in members]
        assert stamps == sorted(stamps)
        if p["type"] == "circular":
            assert all(members[i][1] == members[i + 1][0] for i in range(len(members) - 1))
            assert members[-1][1] == members[0][0]            # closes the loop
            assert len(set(p["roles"]["cycle_accounts"])) == len(members)
        elif p["type"] == "pass_through":
            assert all(members[i][1] == members[i + 1][0] for i in range(len(members) - 1))
            assert members[0][0] == p["roles"]["origin"] and members[-1][1] == p["roles"]["destination"]
        elif p["type"] == "fan_in":
            assert {m[1] for m in members} == {p["roles"]["collector"]} and len({m[0] for m in members}) == len(members)
        else:
            assert {m[0] for m in members} == {p["roles"]["distributor"]} and len({m[1] for m in members}) == len(members)


def test_pattern_sizes_follow_configuration(make_dataset):
    gt = _gt(make_dataset())
    for p in gt["patterns"]:
        n = len(p["transaction_ids"])
        assert {"circular": 3 <= n <= 4, "pass_through": 2 <= n <= 3, "fan_in": 3 <= n <= 4, "fan_out": 3 <= n <= 4}[p["type"]]


def test_ground_truth_lives_in_a_separate_file_and_is_not_referenced_by_ingestion(make_dataset):
    ds = make_dataset()
    cfg_text = ds.ingest_config.read_text()
    assert "ground_truth" not in cfg_text
    for name in ("accounts.csv", "transactions.csv", "alerts.csv", "ingest_config.json"):
        text = (ds.dir / name).read_text()
        assert "pattern" not in text and "circular" not in text and "fan_" not in text and "pass_through" not in text
    manifest_text = (ds.dir / "dataset_manifest.json").read_text()
    for word in ("circular", "fan_in", "fan_out", "pass_through", "cycle_length", "on_pattern_transactions"):
        assert word not in manifest_text           # the manifest only names the redacted sections
    pattern_ids = [p["pattern_id"] for p in _gt(ds)["patterns"]]
    assert not any(pid in (ds.dir / "dataset_manifest.json").read_text() for pid in pattern_ids)
    assert "patterns" not in ds.manifest["configuration"] and "seed_alerts" not in ds.manifest["configuration"]
    assert set(ds.manifest["configuration_redacted_sections"]) == {"patterns", "seed_alerts"}


def test_identifiers_do_not_reveal_pattern_membership(make_dataset):
    ds = make_dataset(seed=3)
    gt = _gt(ds)
    members = {t for p in gt["patterns"] for t in p["transaction_ids"]}
    order = [f"{ds.gen_cfg.namespace}:{t['txn_id']}" for t in ds.rows("transactions.csv")]
    positions = sorted(order.index(t) for t in members)
    assert positions != list(range(positions[0], positions[0] + len(positions)))   # not one contiguous id block


def test_seed_alerts_are_unlabelled_and_valid(make_dataset):
    ds = make_dataset()
    rows = ds.rows("alerts.csv")
    assert len(rows) == 5 and {r["alert_type"] for r in rows} == {"seed"} and {r["severity"] for r in rows} == {"unspecified"}
    flagged = {r["txn_id"] for r in rows}
    assert len(flagged) == len(rows)


def test_manifest_contents(make_dataset):
    m = make_dataset(seed=11).manifest
    assert m["random_seed"] == 11 and m["dataset_namespace"] == "test-ns" and m["generator_version"]
    assert set(m["files"]) >= {"accounts.csv", "transactions.csv", "alerts.csv", "ground_truth.json"}
    assert all(len(v["sha256"]) == 64 for v in m["files"].values())
    assert "PASSWORD" not in json.dumps(m).upper() and m["known_limitations"]


def test_refuses_to_overwrite_without_flag(make_dataset):
    ds = make_dataset()
    with pytest.raises(DatasetConfigError, match="overwrite"):
        write_dataset(ds.gen_cfg, generate(ds.gen_cfg))
    write_dataset(ds.gen_cfg, generate(ds.gen_cfg), overwrite=True)


def test_minor_amount_format_and_zero_exponent_currency(tmp_path):
    raw = generator_dict(out=str(tmp_path / "o"), ingest_out=str(tmp_path / "i"),
                         currency={"code": "JPY", "exponent": 0}, amount_format="minor")
    (tmp_path / "g.json").write_text(json.dumps(raw))
    cfg = load_generator_config(tmp_path / "g.json")
    write_dataset(cfg, generate(cfg))
    out = validate_dataset(load_dataset_config(tmp_path / "o" / "test-ns" / "ingest_config.json"))
    assert out.error_total == 0


@pytest.mark.parametrize("mutate,match", [
    (lambda r: r.pop("seed"), "seed"),
    (lambda r: r.update(seed="x"), "seed"),
    (lambda r: r.update(amount_format="cents"), "amount_format"),
    (lambda r: r["patterns"][0].update(type="smurfing"), "type"),
    (lambda r: r["patterns"][0].update(cycle_length={"min": 2, "max": 3}), "cycle_length"),
    (lambda r: r["patterns"][0].update(bogus=1), "unknown keys"),
    (lambda r: r["patterns"][0].pop("retention_bps"), "retention_bps"),
    (lambda r: r["ordinary_activity"].update(accounts=1), "accounts"),
    (lambda r: r["time_window"].update(start="2026-01-01T00:00:00"), "offset"),
    (lambda r: r["seed_alerts"].pop("alert_type"), "alert_type"),
    (lambda r: r["id_prefixes"].update(account="A:"), "id_prefixes"),
    (lambda r: r.update(extra=1), "unknown keys"),
])
def test_generator_config_validation(mutate, match):
    raw = generator_dict()
    mutate(raw)
    with pytest.raises(DatasetConfigError, match=match):
        parse_generator_config(raw, Path("/x/g.json"))


def test_window_too_short_for_pattern_delays_is_an_error(tmp_path):
    raw = generator_dict(out=str(tmp_path / "o"), ingest_out=str(tmp_path / "i"))
    raw["time_window"]["days"] = 1
    raw["patterns"][0]["delay_seconds"] = {"min": 50000, "max": 80000}
    (tmp_path / "g.json").write_text(json.dumps(raw))
    with pytest.raises(DatasetConfigError, match="too short"):
        generate(load_generator_config(tmp_path / "g.json"))


def test_more_seed_alerts_than_transactions_is_an_error(tmp_path):
    raw = generator_dict(out=str(tmp_path / "o"), ingest_out=str(tmp_path / "i"))
    raw["seed_alerts"]["on_ordinary_transactions"] = 10_000
    (tmp_path / "g.json").write_text(json.dumps(raw))
    with pytest.raises(DatasetConfigError, match="more alerts"):
        generate(load_generator_config(tmp_path / "g.json"))


def test_output_location_does_not_affect_generated_files(tmp_path):
    out = {}
    for name in ("one", "two"):
        raw = generator_dict(out=str(tmp_path / name / "gen"), ingest_out=str(tmp_path / name / "ing"), seed=5)
        (tmp_path / f"{name}.json").write_text(json.dumps(raw))
        cfg = load_generator_config(tmp_path / f"{name}.json")
        write_dataset(cfg, generate(cfg))
        d = cfg.output_dir / "test-ns"
        out[name] = {n: (d / n).read_bytes() for n in ("accounts.csv", "transactions.csv", "alerts.csv", "ground_truth.json")}
        assert str(tmp_path) not in (d / "ground_truth.json").read_text()
        assert str(tmp_path) not in (d / "dataset_manifest.json").read_text()
    assert out["one"] == out["two"]
