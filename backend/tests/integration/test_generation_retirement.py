from typing import Any, cast
# pyright: reportPrivateUsage=false
"""已完成代系在最后一个引用 (包括历史记录) 后必须退役测试."""
import os
import pytest
from sqlalchemy import text, inspect
from entry.migrate import upgrade
from app.infra.schema import check_schema
from tests.integration.test_migrations import postgres

__all__ = ["postgres", "infrastructure", "controller"]

pytestmark = pytest.mark.skipif(os.getenv('RUN_MIGRATION_TESTS') != '1', reason='Opt-in isolated PostgreSQL migrations')


def test_completed_generation_retires_after_last_reference(postgres: Any) -> None:
    upgrade(postgres)
    ids = {"current": "a" * 64, "shared": "b" * 64}
    with postgres.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash) VALUES (1,'generation','x')"))
        db.execute(
            text("INSERT INTO workspaces(id,name,owner_id,container_id) VALUES (1,'generation',1,:shared)"),
            ids,
        )
        db.execute(
            text("INSERT INTO opencode_runtimes(id,workspace_id,user_id,container_id,status) VALUES (1,1,1,:current,'running')"),
            ids,
        )
        for number, key in enumerate(ids, 1):
            db.execute(
                text("INSERT INTO runtime_operations(generation,workspace_id,user_id,runtime_id,candidate_container_id,state,finished_at) VALUES (:generation,1,1,1,:container,'complete',now())"),
                {"generation": str(number) * 32, "container": ids[key]},
            )

    check_schema(postgres)
    with postgres.begin() as db:
        def state(number: Any):
            return db.execute(
                text("SELECT state,finished_at IS NULL FROM runtime_operations WHERE generation=:generation"),
                {"generation": str(number) * 32},
            ).one()

        assert [state(number) for number in (1, 2)] == [
            ("complete", False),
            ("complete", False),
        ]
        assert "ix_runtime_operations_candidate" in {
            index["name"] for index in inspect(db).get_indexes("runtime_operations")
        }
        db.execute(text("UPDATE opencode_runtimes SET status='stopped' WHERE id=1"))
        assert state(1) == ("complete", False)
        savepoint = db.begin_nested()
        db.execute(text("UPDATE opencode_runtimes SET container_id=NULL WHERE id=1"))
        assert state(1) == ("cancelled", True)
        savepoint.rollback()
        assert state(1) == ("complete", False)
        db.execute(text("UPDATE opencode_runtimes SET container_id=:shared WHERE id=1"), ids)
        assert state(1) == ("cancelled", True)
        db.execute(text("DELETE FROM opencode_runtimes WHERE id=1"))
        assert state(2) == ("complete", False)
        db.execute(text("DELETE FROM users WHERE id=1"))
        assert state(2) == ("cancelled", True)


@pytest.mark.skipif(os.getenv("RUN_CONTAINER_DELETION_TESTS") != "1", reason="Opt-in authenticated Controller/Docker")
def test_cancelled_generation_is_retired_after_last_reference_detaches(
    postgres: Any, infrastructure: Any, controller: Any
) -> None:
    import uuid
    from sqlalchemy.orm import sessionmaker
    from app.infra.runtime_operation_cleanup import retire_cancelled_operations
    from runtime_controller.client import ControllerError

    upgrade(postgres)
    identity = int(uuid.uuid4().hex[:6], 16) + 1000
    generation = uuid.uuid4().hex
    infrastructure["cleanup"].append(
        f"skycloud-opencode-w{identity}-u{identity}-g{generation}"
    )
    candidate = controller["client"].prepare(identity, identity, identity, generation)
    candidate_id = candidate["id"]
    with postgres.begin() as db:
        db.execute(
            text("INSERT INTO users(id,username,password_hash) VALUES (:id,'historical','x')"),
            {"id": identity},
        )
        db.execute(
            text("INSERT INTO workspaces(id,name,owner_id) VALUES (:id,'historical',:id)"),
            {"id": identity},
        )
        db.execute(
            text("INSERT INTO opencode_runtimes(id,workspace_id,user_id,container_id,status) VALUES (:id,:id,:id,:container,'running')"),
            {"id": identity, "container": candidate_id},
        )
        db.execute(
            text("INSERT INTO runtime_operations(generation,workspace_id,user_id,runtime_id,candidate_container_id,state,finished_at) VALUES (:generation,:id,:id,:id,:container,'complete',now())"),
            {"generation": generation, "id": identity, "container": candidate_id},
        )
        db.execute(
            text("UPDATE opencode_runtimes SET container_id=NULL WHERE id=:id"),
            {"id": identity},
        )
        db.execute(text("UPDATE runtime_container_deletions SET completed_at=now()"))

    factory = sessionmaker(bind=postgres)
    class Unavailable:
        def retire_generation(self, *args: Any):
            raise ControllerError("unavailable")

    assert retire_cancelled_operations(session_factory=factory, controller_factory=cast(Any, Unavailable)) == 0
    assert controller["client"].inspect(candidate_id)["id"] == candidate_id
    with postgres.begin() as db:
        assert db.execute(
            text("SELECT state,finished_at IS NULL,last_error_type FROM runtime_operations")
        ).one() == ("cancelled", True, "ControllerError")
        db.execute(text("UPDATE runtime_operations SET next_attempt_at=clock_timestamp()"))
    assert retire_cancelled_operations(
        session_factory=factory, controller_factory=lambda: controller["client"]
    ) == 1
    with pytest.raises(ControllerError):
        controller["client"].prepare(identity, identity, identity, generation)
    from tests.integration.test_runtime_security import docker
    assert docker("inspect", candidate_id, check=False).returncode != 0





# 显式导入 fixture, 避免收集过程包含其测试.
from tests.integration.test_runtime_security import infrastructure, controller
