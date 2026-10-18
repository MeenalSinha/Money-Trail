import json

import pytest

from money_trail.cli import main
from money_trail.config import build_settings
from money_trail.exceptions import DatasetConfigError, QueryError
from money_trail.ingestion import report as rpt
from money_trail.investigation import QueryLimits
from money_trail.investigation.queries import QUERY_CEILINGS


class Cfg:  # minimal stand-in carrying only what new_report/write_report read (not a database stand-in)
    namespace, source_name, source_version, source_description = "ns", "src", "1", None
    fingerprint = "f" * 64
    invalid_policy = source_conflict_policy = graph_conflict_policy = accounts_policy = "x"
    batch_size = 5
    report_dir = None


def test_report_roundtrip_listing_and_summary(tmp_path):
    cfg = Cfg(); cfg.report_dir = tmp_path / "reports"
    r = rpt.new_report(cfg, "abc123", "g1")
    r["records"] = {"accounts": {"examined": 3, "accepted": 3, "rejected": 0, "duplicates_identical": 0,
                                 "source_conflicts_skipped": 0}}
    rpt.finish(r, "succeeded")
    path = rpt.write_report(cfg, r)
    assert path.name == "abc123.json" and not list(cfg.report_dir.glob("*.tmp"))
    assert rpt.list_reports(cfg.report_dir)[0]["status"] == "succeeded"
    loaded = rpt.load_report(cfg.report_dir, "abc123")
    assert loaded == json.loads(json.dumps(r))
    text = rpt.render_summary(loaded)
    assert "abc123" in text and "succeeded" in text and "examined 3" in text


def test_report_has_no_secrets_and_rejects_path_traversal(tmp_path):
    cfg = Cfg(); cfg.report_dir = tmp_path
    text = json.dumps(rpt.new_report(cfg, "r1", "g1"))
    assert "password" not in text.lower()
    with pytest.raises(DatasetConfigError):
        rpt.load_report(tmp_path, "../etc/passwd")
    with pytest.raises(DatasetConfigError, match="No report"):
        rpt.load_report(tmp_path, "missing")


def test_query_limits_require_and_ceilings():
    with pytest.raises(QueryError, match="MONEY_TRAIL_QUERY_MAX_RESULTS"):
        QueryLimits().require("max_results", "results")
    assert QueryLimits(max_hops=3).require("max_hops", "hops") == 3
    for bad in (0, -1, True, 1.5):
        with pytest.raises(QueryError):
            QueryLimits(max_hops=bad).require("max_hops", "hops")
    with pytest.raises(QueryError, match="ceiling"):
        QueryLimits(max_hops=QUERY_CEILINGS["hops"] + 1).require("max_hops", "hops")
    assert QueryLimits(max_hops=2).override(max_hops=None, max_paths=4) == QueryLimits(max_hops=2, max_paths=4)


def _env(**over):
    env = {"MONEY_TRAIL_ENV": "development", "FALKORDB_HOST": "h.invalid", "FALKORDB_PORT": "1",
           "FALKORDB_CONNECT_TIMEOUT": "1", "FALKORDB_SOCKET_TIMEOUT": "1", "FALKORDB_GRAPH_NAME": "g"}
    env.update(over)
    return env


def test_query_limit_settings_are_optional_and_validated():
    s = build_settings(_env())
    assert (s.query_max_results, s.query_max_hops) == (None, None)
    s = build_settings(_env(MONEY_TRAIL_QUERY_MAX_RESULTS="50", MONEY_TRAIL_QUERY_MAX_HOPS="4"))
    assert QueryLimits.from_settings(s) == QueryLimits(50, 4, None, None)
    from money_trail.exceptions import ConfigurationError
    for k, v in [("MONEY_TRAIL_QUERY_MAX_RESULTS", "0"), ("MONEY_TRAIL_QUERY_MAX_HOPS", "99"),
                 ("MONEY_TRAIL_QUERY_MAX_PATHS", "abc"), ("MONEY_TRAIL_QUERY_MAX_GAP_MS", "-5")]:
        with pytest.raises(ConfigurationError, match=k):
            build_settings(_env(**{k: v}))


def test_cli_generate_validate_and_overwrite_protection(tmp_path, capsys, make_dataset):
    ds = make_dataset("cli-ns")
    assert main(["validate", "--dataset-config", str(ds.ingest_config)]) == 0
    assert "VALID" in capsys.readouterr().out
    assert main(["validate", "--dataset-config", str(ds.ingest_config), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["errors"]["total"] == 0
    ds.rewrite_csv("transactions.csv", lambda rows: rows[:2] + [["T1", "A", "B", "x", "USD", "bad", "c"]] + rows[2:])
    assert main(["validate", "--dataset-config", str(ds.ingest_config)]) == 1
    assert "INVALID" in capsys.readouterr().out
    gen = ds.dir.parents[1] / "gen.json"
    assert gen.exists()
    assert main(["generate", "--config", str(gen)]) == 1                 # existing output: refused
    assert "overwrite" in capsys.readouterr().err
    assert main(["generate", "--config", str(gen), "--overwrite", "--json"]) == 0


def test_cli_reports_config_errors_without_traceback(tmp_path, capsys):
    assert main(["validate", "--dataset-config", str(tmp_path / "none.json")]) == 1
    assert "not found" in capsys.readouterr().err
    assert main(["report", "--report-dir", str(tmp_path), "list"]) == 0
    assert "no reports" in capsys.readouterr().out


def test_importing_modules_has_no_side_effects(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import importlib, money_trail.cli, money_trail.generation, money_trail.ingestion
    for m in (money_trail.cli, money_trail.generation, money_trail.ingestion):
        importlib.reload(m)
    assert list(tmp_path.iterdir()) == []
