"""The documented commands, end to end, against a disposable real graph."""
import json

import pytest

from money_trail.cli import main


@pytest.fixture
def env_file(tmp_path, test_settings, base_settings):
    s = test_settings
    lines = [f"MONEY_TRAIL_ENV={s.environment}", f"FALKORDB_HOST={s.host}", f"FALKORDB_PORT={s.port}",
             f"FALKORDB_CONNECT_TIMEOUT={s.connect_timeout}", f"FALKORDB_SOCKET_TIMEOUT={s.socket_timeout}",
             f"FALKORDB_GRAPH_NAME={s.graph_name}", f"MONEY_TRAIL_TEST_GRAPH_PREFIX={s.test_graph_prefix}"]
    if s.password:
        lines.append(f"FALKORDB_PASSWORD={s.password}")
    p = tmp_path / "cli.env"
    p.write_text("\n".join(lines))
    return p


def test_cli_workflow(env_file, make_dataset, capsys, conn):
    ds = make_dataset("cli-flow", seed=9)
    env = ["--env-file", str(env_file)]
    assert main(["ingest", "--dataset-config", str(ds.ingest_config), *env]) == 1          # schema not initialized yet
    capsys.readouterr()
    from money_trail.cli import init_main, check_main
    assert init_main(env) == 0 and check_main(env) == 0
    capsys.readouterr()
    assert main(["ingest", "--dataset-config", str(ds.ingest_config), *env, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "succeeded" and report["verification"]["passed"]
    assert main(["ingest", "--dataset-config", str(ds.ingest_config), *env, "--json"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert not again["writes_occurred"]

    assert main(["report", "--dataset-config", str(ds.ingest_config), "--json", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [r["run_id"] for r in listed] and {r["run_id"] for r in listed} >= {report["run_id"], again["run_id"]}
    assert main(["report", "--dataset-config", str(ds.ingest_config), "show", report["run_id"]]) == 0
    assert report["run_id"] in capsys.readouterr().out

    tx = "cli-flow:" + ds.rows("transactions.csv")[0]["txn_id"]
    assert main(["query", "transaction", tx, *env, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and out["data"]["transaction"]["transaction_id"] == tx
    assert main(["query", "transaction", "missing", *env]) == 3                           # not found
    assert main(["query", "account", out["data"]["source_accounts"][0]["account_id"], *env]) == 1   # no limit given
    assert "max_results" in capsys.readouterr().err
    assert main(["query", "account", out["data"]["source_accounts"][0]["account_id"], *env, "--limit", "5"]) == 0
    assert main(["query", "trace", tx, *env, "--max-hops", "3", "--max-paths", "20"]) == 0
    assert main(["query", "evidence", "nope", *env, "--limit", "5"]) == 3
    secret = json.dumps(report)
    assert "password" not in secret.lower()


def test_cli_demo_command(env_file, capsys):
    env = ["--env-file", str(env_file)]
    rc = main(["demo", *env, "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    lines = captured.out.strip().split("\n")
    json_start = -1
    for idx, l in enumerate(lines):
        if l.strip().startswith("{"):
            json_start = idx
            break
    assert json_start != -1
    payload = json.loads("\n".join(lines[json_start:]))
    assert payload["overall"]["status"] == "PASSED"
    assert payload["step_generate"]["status"] == "ok"
    assert payload["step_validate"]["status"] == "ok"
    assert payload["step_ingest"]["status"] == "ok"
    assert payload["step_investigate"]["status"] == "ok"
    assert payload["step_findings"]["status"] == "ok"
    assert payload["step_provenance"]["status"] == "ok"

