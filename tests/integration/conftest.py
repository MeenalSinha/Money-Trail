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


# ---- Phase 4 helpers ---------------------------------------------------------------------------------
@pytest.fixture
def det_config():
    from money_trail.investigation.config import parse_detection_config
    from support import detection_dict
    return parse_detection_config(detection_dict())


@pytest.fixture
def make_agent(ready, det_config):
    """Build an InvestigationAgent over the disposable graph. `provider` is a ScriptedProvider/callable test double."""
    from money_trail.agent.agent import InvestigationAgent
    from money_trail.agent.config import parse_agent_config
    from money_trail.agent.testing import PolicyProvider, ScriptedProvider
    from support import agent_dict

    def _make(provider, agent_over=None, **kw):
        if isinstance(provider, list):
            provider = ScriptedProvider(provider)
        elif callable(provider) and not hasattr(provider, "complete"):
            provider = PolicyProvider(provider)
        cfg = parse_agent_config(agent_dict(**(agent_over or {})))
        kw.setdefault("sleep", lambda s: None)
        return InvestigationAgent(ready, det_config, cfg, provider, **kw), provider
    return _make


@pytest.fixture
def tool_env(ready, det_config):
    from money_trail.agent.config import parse_agent_config
    from money_trail.agent.ledger import Ledger
    from money_trail.agent.tools.handlers import ToolContext
    from money_trail.agent.tools.registry import build_registry
    from money_trail.investigation.engine import InvestigationEngine
    from support import agent_dict

    class Env:
        def __init__(self, over=None):
            self.cfg = parse_agent_config(agent_dict(**(over or {})))
            self.ctx = ToolContext(ready, det_config, self.cfg.tools, InvestigationEngine(ready, det_config))
            self.registry = build_registry(det_config, self.cfg.tools)
            self.ledger = Ledger()

        def call(self, name, **args):
            return self.registry.execute(self.ctx, name, args, self.ledger, self.cfg.loop.max_tool_result_chars)
    return Env


@pytest.fixture
def spy_graph(monkeypatch):
    """Intercept every graph call made through a FalkorDBConnection. `conn.graph` builds a NEW Graph object on each access,
    so patching one instance has no effect; this patches the property on the class. Returns (log, set_hook)."""
    from money_trail.database import FalkorDBConnection
    log, hooks = [], []
    orig = FalkorDBConnection.graph.fget

    class Spy:
        def __init__(self, g):
            self._g = g

        def ro_query(self, q, p=None, **kw):
            log.append(("read", q))
            for h in hooks:
                h(q)
            return self._g.ro_query(q, p, **kw)

        def query(self, q, p=None, **kw):
            log.append(("WRITE", q))
            return self._g.query(q, p, **kw)

        def __getattr__(self, n):
            return getattr(self._g, n)

    monkeypatch.setattr(FalkorDBConnection, "graph", property(lambda self: Spy(orig(self))))
    return log, hooks.append
