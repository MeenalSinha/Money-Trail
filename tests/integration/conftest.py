"""Integration fixtures. These run against a REAL FalkorDB and never skip silently:
if configuration or the database is missing, the tests error with the reason.

Isolation: every test gets its own graph named <MONEY_TRAIL_TEST_GRAPH_PREFIX>_<random>.
Teardown deletes only that graph, and only after re-checking the name.
"""
import os
import uuid
from pathlib import Path

import pytest

from money_trail.config import load_settings
from money_trail.database import FalkorDBConnection
from money_trail.exceptions import ConfigurationError, DatabaseConnectionError


def pytest_collection_modifyitems(items):
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def base_settings():
    env_file = os.environ.get("MONEY_TRAIL_TEST_ENV_FILE") or (".env" if Path(".env").is_file() else None)
    try:
        s = load_settings(env_file)
    except ConfigurationError as exc:
        pytest.fail(f"Integration configuration invalid: {exc}", pytrace=False)
    if not s.test_graph_prefix:
        pytest.fail("MONEY_TRAIL_TEST_GRAPH_PREFIX must be set for integration tests.", pytrace=False)
    return s


@pytest.fixture(scope="session")
def server_check(base_settings):
    """Fail (not skip) when the real database cannot be reached."""
    with FalkorDBConnection(base_settings) as conn:
        try:
            conn.ping()
        except DatabaseConnectionError as exc:
            pytest.fail(f"FalkorDB is not reachable: {exc}", pytrace=False)
    return True


def _unique_name(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _safe_delete(conn: FalkorDBConnection, name: str, prefix: str, protected: str) -> None:
    # Hard guards: never delete anything outside the test namespace or the app graph.
    assert name.startswith(prefix + "_") and name != protected
    if name in conn.client.list_graphs():
        conn.client.select_graph(name).delete()


@pytest.fixture
def test_settings(base_settings, server_check):
    return base_settings.with_graph_name(_unique_name(base_settings.test_graph_prefix))


@pytest.fixture
def conn(test_settings, base_settings):
    c = FalkorDBConnection(test_settings)
    try:
        yield c
    finally:
        try:
            _safe_delete(c, test_settings.graph_name, base_settings.test_graph_prefix, base_settings.graph_name)
        finally:
            c.close()


@pytest.fixture
def make_graph_name(base_settings):
    """Extra disposable graph names in the test namespace (cleaned up after the test)."""
    created = []

    def _make():
        name = _unique_name(base_settings.test_graph_prefix)
        created.append(name)
        return name

    yield _make
    with FalkorDBConnection(base_settings) as c:
        for name in created:
            _safe_delete(c, name, base_settings.test_graph_prefix, base_settings.graph_name)


# --- Phase 2 helpers ----------------------------------------------------------
from money_trail.database import GraphStore, initialize_graph  # noqa: E402
from money_trail.ingestion import ingest_dataset, load_dataset_config  # noqa: E402


@pytest.fixture
def ready(conn):
    """The disposable test graph with the Phase 1 schema initialized."""
    initialize_graph(conn)
    return conn


@pytest.fixture
def run_ingest(ready):
    def _run(ds, **ingestion_overrides):
        if ingestion_overrides:
            ds.edit_ingest(**ingestion_overrides)
        return ingest_dataset(ready, load_dataset_config(ds.ingest_config))
    return _run


@pytest.fixture
def counts(ready):
    def _counts():
        g = ready.graph
        out = {lab: g.ro_query(f"MATCH (n:{lab}) RETURN count(n)").result_set[0][0]
               for lab in ("Account", "Transaction", "Alert")}
        for rel in ("SENT", "RECEIVED_BY", "FLAGS"):
            out[rel] = g.ro_query(f"MATCH ()-[r:{rel}]->() RETURN count(r)").result_set[0][0]
        return out
    return _counts
