"""The documented Phase 3 commands against a disposable real graph."""
import json

import pytest

from money_trail.cli import main
from support import detection_dict
from test_cli import env_file  # noqa: F401  (fixture reuse)


@pytest.fixture
def det_cfg(tmp_path):
    p = tmp_path / "detection.json"
    p.write_text(json.dumps(detection_dict()))
    return str(p)


def run(capsys, *argv):
    code = main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_detectors_commands_are_offline_and_validate(capsys, det_cfg, tmp_path):
    code, out, _ = run(capsys, "detectors", "list")
    assert code == 0 and "circular" in out and "fan_out" in out
    code, out, _ = run(capsys, "detectors", "validate", "--detection-config", det_cfg, "--json")
    data = json.loads(out)
    assert code == 0 and data["rule_fingerprint"] and {d["detector"] for d in data["detectors"]} == {"circular", "pass_through", "fan_in", "fan_out"}
    bad = tmp_path / "bad.json"
    raw = detection_dict()
    del raw["detectors"]["circular"]["max_gap_ms"]
    bad.write_text(json.dumps(raw))
    code, _, err = run(capsys, "detectors", "validate", "--detection-config", str(bad))
    assert code == 1 and "max_gap_ms is required" in err
    code, _, err = run(capsys, "detectors", "validate")
    assert code == 1 and "--detection-config" in err


def test_investigate_persist_findings_and_exit_codes(capsys, env_file, make_dataset, det_cfg, ready):
    from money_trail.cli import init_main
    env = ["--env-file", str(env_file)]
    ds = make_dataset("cli-det", seed=21)
    assert main(["ingest", "--dataset-config", str(ds.ingest_config), *env]) == 0
    capsys.readouterr()
    gt = json.loads((ds.dir / "ground_truth.json").read_text())["patterns"]
    cyc = [p for p in gt if p["type"] == "circular"][0]
    anchor = cyc["transaction_ids"][0]

    code, out, _ = run(capsys, "investigate", "--detection-config", det_cfg, "--transaction-id", anchor, "--persist", "--json", *env)
    res = json.loads(out)
    assert code == 0 and res["status"] == "patterns_detected" and res["complete"] and res["persistence"]["findings"]["created"] >= 2
    inv = res["persistence"]["investigation_id"]
    assert any(set(d["transaction_ids"]) == set(cyc["transaction_ids"]) for d in res["detections"])

    code, out, _ = run(capsys, "investigate", "--detection-config", det_cfg, "--transaction-id", anchor, "--persist", "--json", *env)
    again = json.loads(out)["persistence"]
    assert code == 0 and again["findings"]["created"] == 0 and again["evidence"]["created"] == 0 and again["investigation"]["created"] == 0

    code, out, _ = run(capsys, "investigate", "--detection-config", det_cfg, "--transaction-id", anchor, *env)
    assert code == 0 and "patterns_detected" in out and anchor in out

    code, out, _ = run(capsys, "findings", "list", "--investigation-id", inv, "--limit", "50", "--json", *env)
    listed = json.loads(out)
    assert code == 0 and len(listed["findings"]) >= 2 and {f["kind"] for f in listed["findings"]} == {"observation", "hypothesis"}
    code, out, _ = run(capsys, "findings", "list", "--transaction-id", anchor, "--limit", "50", *env)
    assert code == 0 and "observation" in out
    code, out, _ = run(capsys, "findings", "show", inv, "--limit", "500", "--json", *env)
    shown = json.loads(out)
    assert code == 0 and shown["started_from"] == [anchor] and shown["findings"]
    code, _, err = run(capsys, "findings", "show", "inv:missing", "--limit", "5", *env)
    assert code == 3 and "not found" in err
    code, _, err = run(capsys, "findings", "list", *env)
    assert code == 1 and "max_results" in err
    code, out, _ = run(capsys, "findings", "list", "--limit", "1", *env)
    assert code == 4 and "truncated" in out                      # more findings than the limit: reported as incomplete

    code, _, _ = run(capsys, "investigate", "--detection-config", det_cfg, "--transaction-id", "missing", *env)
    assert code == 3
    code, _, err = run(capsys, "investigate", "--detection-config", det_cfg, "--account-id", "cli-det:x", "--persist", *env)
    assert code == 1 and "Only transaction-anchored" in err
    code, _, err = run(capsys, "investigate", "--detection-config", det_cfg, *env)
    assert code == 1 and "exactly one" in err
    code, out, _ = run(capsys, "investigate", "--detection-config", det_cfg, "--namespace", "cli-det", "--json", *env)
    assert code == 0 and json.loads(out)["status"] == "patterns_detected"

    capped = detection_dict()
    capped["limits"]["max_candidate_paths"] = 1
    p = ds.dir / "capped.json"
    p.write_text(json.dumps(capped))
    code, out, _ = run(capsys, "investigate", "--detection-config", str(p), "--namespace", "cli-det", *env)
    assert code == 4 and "truncated=True" in out

    code, out, _ = run(capsys, "evaluate", "--detection-config", det_cfg, "--namespace", "cli-det",
                       "--ground-truth", str(ds.dir / "ground_truth.json"), "--json", *env)
    rep = json.loads(out)
    assert code == 0 and rep["evaluation"]["per_type"]["circular"]["recall"]["value"] == 1.0
    code, out, _ = run(capsys, "evaluate", "--detection-config", str(p), "--namespace", "cli-det",
                       "--ground-truth", str(ds.dir / "ground_truth.json"), *env)
    assert code == 4 and "truncated searches" in out
    code, _, err = run(capsys, "evaluate", "--detection-config", det_cfg, "--namespace", "cli-det",
                       "--ground-truth", str(ds.dir / "nope.json"), *env)
    assert code == 1 and "not found" in err
    assert "password" not in out.lower()
