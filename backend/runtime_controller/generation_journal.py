"""Durable generation identities and retirement fences in Controller-owned state."""
from contextlib import contextmanager
import fcntl
import os
import re
from pathlib import Path
import sqlite3


class JournalIdentityError(ValueError):
    pass


class GenerationJournal:
    def __init__(self, directory):
        self.directory = Path(directory)

    @contextmanager
    def locked(self, generation, identity):
        if not isinstance(generation,str) or not re.fullmatch(r'[0-9a-f]{32}',generation):
            raise JournalIdentityError('Invalid runtime generation')
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Bounded lock files; shard collisions only serialize unrelated IDs.
        fd = os.open(self.directory / ('generation-' + generation[:2] + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        db = None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            path = self.directory / 'generations.sqlite'
            db = sqlite3.connect(path, timeout=30)
            os.chmod(path,0o600)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            db.execute('''CREATE TABLE IF NOT EXISTS generations (
                generation TEXT PRIMARY KEY, workspace_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL, runtime_id INTEGER NOT NULL,
                state TEXT NOT NULL, container_id TEXT)''')
            row = db.execute('SELECT * FROM generations WHERE generation=?',(generation,)).fetchone()
            if row is None:
                db.execute("INSERT INTO generations VALUES (?,?,?,?,'preparing',NULL)",(generation,*identity))
                db.commit()  # Intent precedes every Docker create, including a crash.
            elif tuple(row[key] for key in ('workspace_id','user_id','runtime_id')) != identity:
                raise JournalIdentityError('Generation belongs to another identity')
            yield GenerationRecord(db,generation)
        finally:
            if db is not None: db.close()
            fcntl.flock(fd,fcntl.LOCK_UN)
            os.close(fd)


class GenerationRecord:
    def __init__(self, db, generation):
        self.db,self.generation = db,generation

    def read(self):
        return self.db.execute('SELECT * FROM generations WHERE generation=?',(self.generation,)).fetchone()

    def ready(self, container_id):
        self.db.execute("UPDATE generations SET state='ready',container_id=? WHERE generation=?",(container_id,self.generation))
        self.db.commit()

    def retire(self):
        # Commit the fence before removal. Failure must leave it retired so a
        # delayed prepare can never recreate a deleted resource.
        self.db.execute("UPDATE generations SET state='retired' WHERE generation=?",(self.generation,))
        self.db.commit()
