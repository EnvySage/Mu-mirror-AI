from uuid import UUID

import pytest

from agent_data.scope import ReadScope, bounded_limit


def test_read_scope_accepts_complete_authenticated_scope():
    scope = ReadScope(
        UUID("00000000-0000-0000-0000-000000000001"),
        UUID("10000000-0000-0000-0000-000000000001"),
        10,
    )

    assert scope.user_message_id == 10


@pytest.mark.parametrize(
    "values",
    [
        (None, UUID("10000000-0000-0000-0000-000000000001"), 10),
        (UUID("00000000-0000-0000-0000-000000000001"), None, 10),
        (UUID("00000000-0000-0000-0000-000000000001"), UUID("10000000-0000-0000-0000-000000000001"), None),
    ],
)
def test_read_scope_rejects_missing_fields(values):
    with pytest.raises(ValueError):
        ReadScope(*values)


@pytest.mark.parametrize("message_id", [0, -1, True, "10"])
def test_read_scope_rejects_invalid_message_boundary(message_id):
    with pytest.raises((TypeError, ValueError)):
        ReadScope(
            UUID("00000000-0000-0000-0000-000000000001"),
            UUID("10000000-0000-0000-0000-000000000001"),
            message_id,
        )


def test_limit_is_positive_and_clamped_to_repository_maximum():
    assert bounded_limit(200, maximum=20, default=20) == 20
    with pytest.raises(ValueError):
        bounded_limit(0, maximum=20, default=20)
