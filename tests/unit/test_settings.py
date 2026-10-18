import pytest

from money_trail.config import build_settings, load_settings
from money_trail.exceptions import ConfigurationError

SECRET = "unit-test-only-secret-value"


def valid_env(**over):
    env = {
        "MONEY_TRAIL_ENV": "development",
        "FALKORDB_HOST": "db.example.invalid",
        "FALKORDB_PORT": "6380",
        "FALKORDB_PASSWORD": SECRET,
        "FALKORDB_CONNECT_TIMEOUT": "5",
        "FALKORDB_SOCKET_TIMEOUT": "10",
        "FALKORDB_GRAPH_NAME": "graph_one",
    }
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


def test_valid_configuration():
    s = build_settings(valid_env())
    assert (s.host, s.port, s.graph_name, s.tls) == ("db.example.invalid", 6380, "graph_one", False)
    assert s.log_level == "INFO"


@pytest.mark.parametrize("key", [
    "MONEY_TRAIL_ENV", "FALKORDB_HOST", "FALKORDB_PORT", "FALKORDB_GRAPH_NAME",
    "FALKORDB_CONNECT_TIMEOUT", "FALKORDB_SOCKET_TIMEOUT",
])
def test_missing_required_setting(key):
    with pytest.raises(ConfigurationError) as e:
        build_settings(valid_env(**{key: None}))
    assert key in str(e.value)


def test_all_problems_reported_together():
    with pytest.raises(ConfigurationError) as e:
        build_settings({})
    msg = str(e.value)
    assert "FALKORDB_HOST" in msg and "FALKORDB_PORT" in msg and "FALKORDB_GRAPH_NAME" in msg


@pytest.mark.parametrize("port", ["abc", "0", "70000", "-1", "63.5"])
def test_invalid_port(port):
    with pytest.raises(ConfigurationError, match="FALKORDB_PORT"):
        build_settings(valid_env(FALKORDB_PORT=port))


@pytest.mark.parametrize("value", ["abc", "0", "-3", "9999"])
def test_invalid_timeout(value):
    with pytest.raises(ConfigurationError, match="FALKORDB_CONNECT_TIMEOUT"):
        build_settings(valid_env(FALKORDB_CONNECT_TIMEOUT=value))


@pytest.mark.parametrize("name", ["1abc", "has space", "semi;colon", "a" * 64, "back`tick", "x-y"])
def test_invalid_graph_name(name):
    with pytest.raises(ConfigurationError, match="FALKORDB_GRAPH_NAME"):
        build_settings(valid_env(FALKORDB_GRAPH_NAME=name))


def test_invalid_environment_and_bool_and_log_level():
    for key, value in [("MONEY_TRAIL_ENV", "staging"), ("FALKORDB_TLS", "maybe"),
                       ("MONEY_TRAIL_LOG_LEVEL", "LOUD")]:
        with pytest.raises(ConfigurationError, match=key):
            build_settings(valid_env(**{key: value}))


def test_username_requires_password():
    with pytest.raises(ConfigurationError, match="FALKORDB_PASSWORD"):
        build_settings(valid_env(FALKORDB_USERNAME="svc", FALKORDB_PASSWORD=None))


def test_placeholders_are_rejected():
    with pytest.raises(ConfigurationError, match="FALKORDB_HOST"):
        build_settings(valid_env(FALKORDB_HOST="<falkordb-host>"))
    with pytest.raises(ConfigurationError, match="placeholder"):
        build_settings(valid_env(FALKORDB_PASSWORD="<set-a-strong-password>"))


def test_tls_ca_cert_requires_tls_and_existing_file(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    with pytest.raises(ConfigurationError, match="TLS is not enabled"):
        build_settings(valid_env(FALKORDB_TLS_CA_CERT=str(ca)))
    with pytest.raises(ConfigurationError, match="existing file"):
        build_settings(valid_env(FALKORDB_TLS="true", FALKORDB_TLS_CA_CERT=str(tmp_path / "nope.pem")))
    assert build_settings(valid_env(FALKORDB_TLS="true", FALKORDB_TLS_CA_CERT=str(ca))).tls is True


def test_production_requires_password_and_tls():
    with pytest.raises(ConfigurationError) as e:
        build_settings(valid_env(MONEY_TRAIL_ENV="production", FALKORDB_PASSWORD=None))
    assert "Production requires FALKORDB_PASSWORD" in str(e.value)
    assert "Production requires FALKORDB_TLS" in str(e.value)
    ok = build_settings(valid_env(MONEY_TRAIL_ENV="production", FALKORDB_TLS="true"))
    assert ok.environment == "production"


def test_secret_never_in_repr_or_errors():
    s = build_settings(valid_env())
    assert SECRET not in repr(s) and SECRET not in str(s) and SECRET not in str(s.safe_summary())
    with pytest.raises(ConfigurationError) as e:
        build_settings(valid_env(FALKORDB_PORT="bad"))
    assert SECRET not in str(e.value)


def test_test_prefix_must_differ_from_graph_name():
    with pytest.raises(ConfigurationError, match="must differ"):
        build_settings(valid_env(MONEY_TRAIL_TEST_GRAPH_PREFIX="graph_one"))


def test_with_graph_name_is_isolated_copy():
    base = build_settings(valid_env())
    other = base.with_graph_name("graph_two")
    assert base.graph_name == "graph_one" and other.graph_name == "graph_two"
    with pytest.raises(ConfigurationError):
        base.with_graph_name("bad name")


def test_load_settings_env_file_and_precedence(tmp_path):
    f = tmp_path / "t.env"
    f.write_text("\n".join(f"{k}={v}" for k, v in valid_env().items()))
    s = load_settings(f, environ={"FALKORDB_PORT": "7000", "UNRELATED": "x"})
    assert s.port == 7000 and s.host == "db.example.invalid"


def test_load_settings_missing_file(tmp_path):
    with pytest.raises(ConfigurationError, match="not found"):
        load_settings(tmp_path / "missing.env", environ={})


def test_no_implicit_defaults_for_endpoint_or_graph():
    with pytest.raises(ConfigurationError):
        load_settings(None, environ={})
