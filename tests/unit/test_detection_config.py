import copy
import json
from pathlib import Path

import pytest

from money_trail.exceptions import DatasetConfigError
from money_trail.investigation.config import load_detection_config, parse_detection_config
from support import detection_dict


def parse(mutate=None):
    raw = detection_dict()
    if mutate:
        mutate(raw)
    return parse_detection_config(raw)


def test_valid_config_and_shipped_example():
    c = parse()
    assert c.enabled_names() == ("circular", "pass_through", "fan_in", "fan_out")
    ex = load_detection_config(Path(__file__).resolve().parents[2] / "examples" / "detection.example.json")
    assert ex.rule_set_name == "demo-rules" and ex.limits.max_candidate_paths > 0


@pytest.mark.parametrize("path", [
    ("rule_set",), ("limits",), ("detectors",), ("persistence",), ("evaluation",),
    ("rule_set", "version"), ("limits", "max_candidate_paths"), ("limits", "max_evidence_transactions"),
    ("limits", "query_timeout_ms"), ("persistence", "hypothesis_findings"), ("evaluation", "match"),
    ("detectors", "circular"), ("detectors", "fan_in", "enabled"),
    ("detectors", "circular", "min_hops"), ("detectors", "circular", "max_gap_ms"),
    ("detectors", "circular", "max_total_window_ms"), ("detectors", "circular", "amount_retention"),
    ("detectors", "circular", "allow_repeated_accounts"), ("detectors", "pass_through", "retention"),
    ("detectors", "fan_in", "window_ms"), ("detectors", "fan_out", "min_distinct_counterparties"),
    ("detectors", "fan_out", "min_total_amount"),
])
def test_every_setting_is_required(path):
    def rm(raw):
        d = raw
        for k in path[:-1]:
            d = d[k]
        del d[path[-1]]
    with pytest.raises(DatasetConfigError):
        parse(rm)


@pytest.mark.parametrize("mutate,match", [
    (lambda r: r.update(surprise=1), "unknown keys"),
    (lambda r: r["limits"].update(extra=1), "unknown keys"),
    (lambda r: r["detectors"].update(smurfing={"enabled": True}), "unknown detector"),
    (lambda r: r["detectors"]["circular"].update(bogus=1), "unknown keys"),
    (lambda r: r["detectors"]["circular"].update(min_hops=1), "min_hops"),
    (lambda r: r["detectors"]["circular"].update(min_hops=6, max_hops=5), "min_hops must not exceed"),
    (lambda r: r["detectors"]["circular"].update(max_hops=11), "max_hops"),
    (lambda r: r["detectors"]["circular"].update(max_gap_ms=-1), "max_gap_ms"),
    (lambda r: r["detectors"]["circular"].update(max_gap_ms=1.5), "max_gap_ms"),
    (lambda r: r["detectors"]["circular"].update(max_gap_ms=True), "max_gap_ms"),
    (lambda r: r["detectors"]["circular"].update(allow_repeated_accounts="no"), "true or false"),
    (lambda r: r["detectors"]["circular"].update(amount_retention={"min_bps": 5}), "max_bps"),
    (lambda r: r["detectors"]["circular"].update(amount_retention={"min_bps": 9000, "max_bps": 100}), "must not exceed"),
    (lambda r: r["detectors"]["pass_through"].update(retention=0.9), "null or an object"),
    (lambda r: r["detectors"]["fan_in"].update(min_distinct_counterparties=1), "min_distinct_counterparties"),
    (lambda r: r["detectors"]["fan_in"].update(min_transactions=2), "at least min_distinct"),
    (lambda r: r["detectors"]["fan_out"].update(min_total_amount={"currency": "usd", "minor": 5}), "3-letter"),
    (lambda r: r["detectors"]["fan_out"].update(min_total_amount={"currency": "USD"}), "minor"),
    (lambda r: r["limits"].update(max_candidate_paths=0), "max_candidate_paths"),
    (lambda r: r["limits"].update(max_candidate_paths=10**9), "max_candidate_paths"),
    (lambda r: r["limits"].update(query_timeout_ms=0), "query_timeout_ms"),
    (lambda r: r["limits"].update(max_evidence_transactions=3), "max_evidence_transactions"),
    (lambda r: r["rule_set"].update(name="a@b"), "rule_set.name"),
    (lambda r: r["evaluation"]["match"].update(min_gt_coverage_percent=0), "min_gt_coverage_percent"),
    (lambda r: r["evaluation"]["match"].update(min_detection_purity_percent=101), "min_detection_purity_percent"),
    (lambda r: r["persistence"].update(hypothesis_findings=1), "true or false"),
])
def test_invalid_values_are_rejected_with_actionable_messages(mutate, match):
    with pytest.raises(DatasetConfigError, match=match):
        parse(mutate)


def test_disabled_detector_needs_only_the_flag_but_partial_settings_are_still_validated():
    c = parse(lambda r: r["detectors"].update(fan_in={"enabled": False}))
    assert "fan_in" not in c.enabled_names()
    with pytest.raises(DatasetConfigError):
        parse(lambda r: r["detectors"].update(fan_in={"enabled": False, "window_ms": -5}))


def test_timeout_can_be_explicitly_disabled():
    assert parse(lambda r: r["limits"].update(query_timeout_ms=None)).limits.query_timeout_ms is None


def test_rule_version_tracks_thresholds_and_caps_but_not_timeouts_or_evaluation():
    base = parse()
    v = base.rule_version("circular")
    assert v.startswith("circular:test-rules@1+")
    assert parse().rule_version("circular") == v                                   # deterministic
    assert parse(lambda r: r["detectors"]["circular"].update(max_gap_ms=1)).rule_version("circular") != v
    assert parse(lambda r: r["limits"].update(max_candidate_paths=7)).rule_version("circular") != v
    assert parse(lambda r: r["limits"].update(query_timeout_ms=5)).rule_version("circular") == v
    assert parse(lambda r: r["evaluation"]["match"].update(min_gt_coverage_percent=50)).rule_version("circular") == v
    # changing one detector does not change another's version
    changed = parse(lambda r: r["detectors"]["circular"].update(max_gap_ms=1))
    assert changed.rule_version("fan_in") == base.rule_version("fan_in")
    assert changed.fingerprint() != base.fingerprint()
    assert parse(lambda r: r["rule_set"].update(version="2")).rule_version("fan_in") != base.rule_version("fan_in")


def test_load_errors(tmp_path):
    with pytest.raises(DatasetConfigError, match="not found"):
        load_detection_config(tmp_path / "x.json")
    (tmp_path / "bad.json").write_text("{nope")
    with pytest.raises(DatasetConfigError, match="not valid JSON"):
        load_detection_config(tmp_path / "bad.json")
    (tmp_path / "arr.json").write_text("[]")
    with pytest.raises(DatasetConfigError, match="JSON object"):
        load_detection_config(tmp_path / "arr.json")
