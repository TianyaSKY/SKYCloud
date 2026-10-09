"""Serialize stream persistence in one worker; ORM values stay on that worker."""
import asyncio
import anyio
from concurrent.futures import ThreadPoolExecutor

from app.exceptions import ConflictError, ResourceNotFoundError
from app.features.workspace.permissions import assert_member, assert_can_write, get_member_role
from app.features.assistant import repository, abort_repository
from app.models.assistant import AssistantMessage, AssistantRun


class StreamWorker:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='assistant-db')

    async def call(self, function, *args):
        future = asyncio.wrap_future(self.executor.submit(function, *args))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # A running SQL operation cannot be cancelled. Drain it before
            # cleanup or another operation can touch the same Session.
            with anyio.CancelScope(shield=True):
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
            if future.done() and not future.cancelled():
                future.exception()
            raise

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.executor.shutdown(wait=False)


class StreamPersistence:
    def __init__(self, session, workspace, user_id, conversation, run):
        self.session = session
        self.workspace = workspace
        self.user_id = int(user_id)
        self.conversation = conversation
        self.run = run
        if (run.conversation_id != conversation.id or conversation.workspace_id != workspace.id
                or conversation.user_id != self.user_id):
            raise ResourceNotFoundError('Assistant run not found')
        assert_member(session, int(workspace.id), self.user_id)
        if conversation.mode == 'expert':
            assert_can_write(session, int(workspace.id), self.user_id)
        self.run_id = int(run.id)
        self.credential = None
        self.message = session.get(AssistantMessage, run.assistant_message_id)
        if self.message is None:
            raise ResourceNotFoundError('Assistant message not found')
        self.message_id = int(self.message.id)
        self.workspace_id = int(workspace.id)
        self.mode = conversation.mode
        user_message = session.get(AssistantMessage, run.user_message_id)
        if user_message is None:
            raise ResourceNotFoundError('Assistant user message not found')
        self.query = str(user_message.content)

    def initialize(self, now):
        if self.credential is not None:
            self.credential.validate(self.session, self.user_id)
        if not repository.touch_run_lease(self.session, self.run_id, now):
            raise ConflictError('任务已结束，不能继续执行')
        return self.run_id, self.message_id, self.workspace_id, self.mode, self.query

    def poll(self, renew_lease, now):
        if self.credential is not None:
            self.credential.validate(self.session, self.user_id)
        role = get_member_role(self.session, self.workspace_id, self.user_id)
        revoked = role is None or (self.mode == 'expert' and role not in {'editor', 'admin'})
        if renew_lease and not repository.touch_run_lease(self.session, self.run_id, now):
            return True, True
        row = self.session.query(AssistantRun.status, AssistantRun.cancel_requested).filter(
            AssistantRun.id == self.run_id).first()
        return row is None or row[0] in repository.TERMINAL_RUN_STATUSES, revoked or bool(row and row[1])

    def checkpoint(self, content, metadata, usage, permission, commit):
        if permission is not None:
            run = repository.lock_run(self.session, self.run_id)
            if run is None or run.status in repository.TERMINAL_RUN_STATUSES:
                self.session.commit()
                return False
            # ExpertEngine already persisted this event. A recovery client may
            # have answered before the stream consumes it; never reset its CAS.
            if run.pending_permission_id != permission.get('permission_id'):
                repository.set_pending_permission(run, permission)
            commit = True
        self.message.content = content
        self.message.metadata_dict = dict(metadata)
        self.message.usage = dict(usage)
        self.session.flush()
        if commit:
            self.session.commit()
        return True

    def finish(self, status, content, metadata, usage, events, error_code=None):
        # Recover from a failed flush/commit and refresh any state changed by
        # another API worker before deciding the terminal transition.
        self.session.rollback()
        run = repository.lock_run(self.session, self.run_id)
        if run is None:
            self.session.commit()
            return None
        if run.status in repository.TERMINAL_RUN_STATUSES:
            if run.status in {"failed", "cancelled"}:
                abort_repository.stage(self.session, run)
            result = run.status
            self.session.commit()
            return result
        if run.cancel_requested:
            status = 'cancelled'
        if status in {'failed', 'cancelled'}:
            abort_repository.stage(self.session, run)
        self.message.content = content
        self.message.status = status
        self.message.metadata_dict = dict(metadata)
        self.message.usage = dict(usage)
        run.metadata_dict = repository.serialize_events_metadata(events)
        repository.update_run_status(run, status, error_code=error_code,
            error_message=('专家任务执行失败' if self.mode == 'expert' else '快速问答执行失败')
            if status == 'failed' else None)
        self.session.commit()
        return status

    def engine(self):
        if self.mode == 'fast':
            # Imported here to preserve the service's injectable engine seam.
            from app.features.assistant.service import FastEngine
            return FastEngine()
        from app.features.assistant.engines.expert_engine import ExpertEngine
        return ExpertEngine(session=self.session, conversation=self.conversation, run=self.run)
