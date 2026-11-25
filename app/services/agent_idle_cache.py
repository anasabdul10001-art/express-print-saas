"""
Lets the print agent's frequent "anything to print?" poll be answered without
touching the database while there is nothing to do.

After the database said "no job", that answer is remembered in memory for some
hours. It is forgotten at once when this process commits a new or changed print
job, or a print agent is disabled or deleted. The time limit covers anything that
changes the database from outside this process (a second instance, bulk updates),
so such a job can wait up to that limit (a restart or deploy also forgets).
"""

import threading
import time

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.models.print_agent import PrintAgent
from app.models.print_job import PrintJob

# Long on purpose: a database that is billed while awake only sleeps if nothing asks it
# every few minutes.
IDLE_SECONDS = 6 * 60 * 60

_lock = threading.Lock()
_idle: dict[tuple[str, str], float] = {}  # (agent id, API key hash) -> valid until
_generation = 0
_FLAG = "print_work_changed"


def generation() -> int:
    """Read before asking the database, and pass to remember()."""
    return _generation


def is_idle(agent_id: str, key_hash: str) -> bool:
    with _lock:
        return _idle.get((agent_id, key_hash), 0) > time.monotonic()


def remember(agent_id: str, key_hash: str, seen_generation: int) -> None:
    """Only for an authenticated agent that had no job. Ignored if something changed
    while the database was being asked."""
    with _lock:
        if seen_generation == _generation:
            _idle[(agent_id, key_hash)] = time.monotonic() + IDLE_SECONDS


def forget() -> None:
    global _generation
    with _lock:
        _generation += 1
        _idle.clear()


def _matters(session) -> bool:
    for item in list(session.new) + list(session.dirty) + list(session.deleted):
        if isinstance(item, PrintJob):
            return True
        if isinstance(item, PrintAgent):
            if item in session.deleted:
                return True
            state = inspect(item)
            if state.attrs.api_key_hash.history.has_changes():
                return True
            if state.attrs.status.history.has_changes() and item.status == "disabled":
                return True
    return False


@event.listens_for(Session, "before_flush")
def _note_changes(session, flush_context, instances):
    if _matters(session):
        session.info[_FLAG] = True


@event.listens_for(Session, "after_commit")
def _forget_after_commit(session):
    if session.info.pop(_FLAG, False):
        forget()


@event.listens_for(Session, "after_rollback")
def _drop_flag(session):
    session.info.pop(_FLAG, None)
