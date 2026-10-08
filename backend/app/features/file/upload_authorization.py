"""Current edit permission at upload admission and SQL publication boundaries."""
from app.features.file import upload_quota
from app.features.workspace.permissions import assert_can_write_locked


def reserve(session, workspace_id, user_id, size, **kwargs):
    assert_can_write_locked(session, workspace_id, user_id)
    return upload_quota.reserve(session, workspace_id, user_id, size, **kwargs)


def consume(session, workspace_id, user_id, reservation_id, size):
    # The caller publishes File/outbox in this transaction, without an
    # intervening commit. A role mutation must wait for that outcome.
    assert_can_write_locked(session, workspace_id, user_id)
    return upload_quota.consume(session, workspace_id, user_id, reservation_id, size)
