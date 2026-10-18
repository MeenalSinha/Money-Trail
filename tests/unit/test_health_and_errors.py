import json
from unittest import mock

import pytest
from redis import exceptions as redis_exc

from money_trail.config import build_settings
from money_trail.database import FalkorDBConnection, check_health
from money_trail.database import falkordb_client as fc
from money_trail.database import health as h
from money_trail.exceptions import ConfigurationError, DatabaseConnectionError

SECRET = "unit-test-only-secret-value"


@pytest.fixture
def settings():
    return build_settings({
        "MONEY_TRAIL_ENV": "development", "FALKORDB_HOST": "db.example.invalid",
        "FALKORDB_PORT": "6380", "FALKORDB_PASSWORD": SECRET,
        "FALKORDB_CONNECT_TIMEOUT": "1", "FALKORDB_SOCKET_TIMEOUT": "1",
        "FALKORDB_GRAPH_NAME": "g1",
    })


@pytest.mark.parametrize("exc,needle", [
    (redis_exc.AuthenticationError("raw " + SECRET), "credentials"),
    (redis_exc.TimeoutError("raw"), "in time"),
    (redis_exc.ConnectionError("raw " + SECRET), "Could not connect"),
    (RuntimeError("raw " + SECRET), "RuntimeError"),
])
def test_error_translation_is_actionable_and_secret_free(exc, needle):
    out = fc._translate(exc)
    assert needle in str(out) and SECRET not in str(out)


def test_unreachable_database_is_reported_not_faked(settings):
    conn = FalkorDBConnection(settings)
    with mock.patch.object(FalkorDBConnection, "client", new_callable=mock.PropertyMock) as m:
        m.return_value.connection.ping.side_effect = redis_exc.ConnectionError("down")
        report = check_health(conn)
    assert report.status == h.UNREACHABLE and not report.healthy
    assert not report.database_reachable and not report.schema_ready
    assert SECRET not in json.dumps(report.to_dict())


def test_health_report_formatting_has_no_endpoint_or_secret(settings):
    r = h.HealthReport(status=h.OK, configuration_valid=True, details=settings.safe_summary())
    text = json.dumps(r.to_dict())
    assert SECRET not in text and "db.example.invalid" not in text and "6380" not in text
    assert r.healthy


def test_config_error_report():
    r = h.report_configuration_error(ConfigurationError("bad"))
    assert r.status == h.CONFIG_ERROR and not r.configuration_valid and not r.healthy


def test_graph_missing_is_distinct_from_unreachable(settings):
    conn = FalkorDBConnection(settings)
    with mock.patch.object(FalkorDBConnection, "client", new_callable=mock.PropertyMock) as m:
        m.return_value.list_graphs.return_value = ["other"]
        report = check_health(conn)
    assert report.status == h.GRAPH_MISSING and report.database_reachable
