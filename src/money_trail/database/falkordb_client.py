"""FalkorDB connection management.

One FalkorDBConnection owns one client (and its connection pool) for the
configured graph. Credentials are taken from Settings and never logged.
"""
from __future__ import annotations

import logging
from typing import Optional

from falkordb import FalkorDB, Graph
from redis import exceptions as redis_exc

from money_trail.config import Settings
from money_trail.exceptions import DatabaseConnectionError

log = logging.getLogger(__name__)


def _translate(exc: Exception) -> DatabaseConnectionError:
    """Map client errors to a safe, actionable error (no secrets, no raw messages)."""
    if isinstance(exc, redis_exc.AuthenticationError):
        return DatabaseConnectionError(
            "FalkorDB rejected the credentials. Check FALKORDB_USERNAME and FALKORDB_PASSWORD."
        )
    if isinstance(exc, redis_exc.TimeoutError):
        return DatabaseConnectionError(
            "FalkorDB did not respond in time. Check network reachability or raise "
            "FALKORDB_CONNECT_TIMEOUT / FALKORDB_SOCKET_TIMEOUT."
        )
    if isinstance(exc, redis_exc.ConnectionError):
        return DatabaseConnectionError(
            "Could not connect to FalkorDB. Check that it is running and that "
            "FALKORDB_HOST, FALKORDB_PORT and FALKORDB_TLS match the server."
        )
    return DatabaseConnectionError(f"Unexpected FalkorDB client error ({type(exc).__name__}).")


class FalkorDBConnection:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Optional[FalkorDB] = None

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def client(self) -> FalkorDB:
        if self._client is None:
            s = self._settings
            kwargs = dict(
                host=s.host,
                port=s.port,
                password=s.password,
                username=s.username,
                socket_connect_timeout=s.connect_timeout,
                socket_timeout=s.socket_timeout,
            )
            if s.tls:
                kwargs.update(ssl=True, ssl_ca_certs=s.tls_ca_cert)
            try:
                self._client = FalkorDB(**kwargs)
            except Exception as exc:  # client construction can open a connection
                raise _translate(exc) from None
            log.info("FalkorDB client created for graph %s", s.graph_name)
        return self._client

    def ping(self) -> None:
        """Send PING to the server; raises DatabaseConnectionError on failure."""
        try:
            self.client.connection.ping()
        except DatabaseConnectionError:
            raise
        except Exception as exc:
            raise _translate(exc) from None

    def graph_exists(self) -> bool:
        try:
            return self._settings.graph_name in self.client.list_graphs()
        except DatabaseConnectionError:
            raise
        except Exception as exc:
            raise _translate(exc) from None

    @property
    def graph(self) -> Graph:
        """Handle to the configured graph (always settings.graph_name)."""
        return self.client.select_graph(self._settings.graph_name)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    def __enter__(self) -> "FalkorDBConnection":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
