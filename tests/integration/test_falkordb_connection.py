import socket

import pytest

from money_trail.database import FalkorDBConnection
from money_trail.exceptions import DatabaseConnectionError


def test_a_real_connection_executes_real_operations(conn):
    conn.ping()
    # a real query round trip on the (not yet created) disposable graph's server
    rows = conn.client.select_graph(conn.settings.graph_name).query("RETURN 1 + 1").result_set
    assert rows == [[2]]


def test_graph_handle_is_the_configured_graph(conn, test_settings):
    assert conn.graph.name == test_settings.graph_name


def test_g_unreachable_port_reports_failure(base_settings):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    bad = base_settings.__class__(**{**base_settings.__dict__, "host": "127.0.0.1", "port": free_port,
                                     "connect_timeout": 1.0})
    with FalkorDBConnection(bad) as c, pytest.raises(DatabaseConnectionError) as e:
        c.ping()
    assert "Could not connect" in str(e.value) or "did not respond in time" in str(e.value)
    assert (base_settings.password or "<none>") not in str(e.value)


def test_g_wrong_password_reports_auth_failure(base_settings, server_check):
    if not base_settings.password:
        pytest.skip("Server under test has no password; wrong-password path needs an authenticated server "
                    "(the documented compose setup always sets one).")
    bad = base_settings.__class__(**{**base_settings.__dict__, "password": "definitely-wrong-password"})
    with FalkorDBConnection(bad) as c, pytest.raises(DatabaseConnectionError) as e:
        c.ping()
    assert "credentials" in str(e.value)
    assert "definitely-wrong-password" not in str(e.value)
