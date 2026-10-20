"""Fixtures for the isolated graph_v2 PostgreSQL integration database."""

from __future__ import annotations

import os
from uuid import UUID

import pytest
from psycopg_pool import ConnectionPool

from agent_data.pool import create_read_pool
from agent_data.scope import ReadScope


USER_A = UUID("00000000-0000-0000-0000-000000000001")
USER_B = UUID("00000000-0000-0000-0000-000000000002")
SESSION_A = UUID("10000000-0000-0000-0000-000000000001")
SESSION_B = UUID("10000000-0000-0000-0000-000000000002")
CURRENT_USER_MESSAGE_ID = 904


@pytest.fixture(scope="session")
def read_dsn() -> str:
    dsn = os.environ.get("MIRROR_TEST_READ_DSN")
    if not dsn:
        pytest.skip("MIRROR_TEST_READ_DSN is required for PostgreSQL integration checks")
    return dsn


@pytest.fixture(scope="session")
def agent_pool(read_dsn: str) -> ConnectionPool:
    pool = create_read_pool(read_dsn)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture
def read_scope() -> ReadScope:
    return ReadScope(
        user_id=USER_A,
        session_id=SESSION_A,
        user_message_id=CURRENT_USER_MESSAGE_ID,
    )
