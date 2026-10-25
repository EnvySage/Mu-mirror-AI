"""Short-lived, bounded PostgreSQL read transactions for agent data access."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from psycopg import Connection
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

from agent_data.scope import ReadScope, require_scope


POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 4
POOL_TIMEOUT_SECONDS = 1.0
MAX_WAITING = 4
CONNECT_TIMEOUT_SECONDS = 3
STATEMENT_TIMEOUT = "3000ms"
LOCK_TIMEOUT = "1000ms"


def create_read_pool(dsn: str) -> ConnectionPool:
    """Create the dedicated read pool without logging or persisting the DSN."""
    if not isinstance(dsn, str) or not dsn.strip():
        raise ValueError("a non-empty PostgreSQL DSN is required")

    pool = ConnectionPool(
        conninfo=dsn,
        min_size=POOL_MIN_SIZE,
        max_size=POOL_MAX_SIZE,
        timeout=POOL_TIMEOUT_SECONDS,
        max_waiting=MAX_WAITING,
        kwargs={"connect_timeout": CONNECT_TIMEOUT_SECONDS, "row_factory": dict_row},
        name="mirror-agent-read",
        open=False,
    )
    try:
        pool.open(wait=True, timeout=CONNECT_TIMEOUT_SECONDS + 2)
    except Exception:
        pool.close()
        raise
    return pool


@contextmanager
def read_transaction(pool: ConnectionPool, scope: ReadScope) -> Iterator[Connection]:
    """Borrow one connection, enforce read-only and short DB timeouts, then return it."""
    require_scope(scope)
    with pool.connection(timeout=POOL_TIMEOUT_SECONDS) as connection:
        with connection.transaction():
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute(
                "SELECT set_config('statement_timeout', %s, true), "
                "set_config('lock_timeout', %s, true)",
                (STATEMENT_TIMEOUT, LOCK_TIMEOUT),
            )
            yield connection
