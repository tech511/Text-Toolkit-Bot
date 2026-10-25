"""Per-user task queue shared by /qrcode, /tts, /translate and /txtfile.

NOTE: this file is intentionally NOT called ``queue.py``. A top-level
``queue.py`` would shadow Python's built-in ``queue`` module and break
``asyncio`` / ``concurrent.futures`` (and therefore Pyrogram).

How it works
------------
* Every user owns a private FIFO queue (``MAX_USER_TASKS`` tasks max).
* ``MAX_RUNNING`` workers run jobs at the same time. Workers take the next
  job round-robin across users, so one user can never starve the others.
* A user never has two jobs running at once (their second job waits).
* ``MAX_RUNNING + MAX_WAITING`` is the global capacity; beyond it new
  requests are rejected with ``QueueFull``.
* Every job has a hard timeout; errors, timeouts and Telegram failures are
  caught so a worker can never die because of one bad job.
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from config import (
    MAX_RUNNING,
    MAX_WAITING,
    MAX_USER_TASKS,
    QUEUE_TASK_TIMEOUT,
)

log = logging.getLogger("task_queue")

Job = Callable[[], Awaitable[None]]
Hook = Callable[[], Awaitable[None]]
ErrorHook = Callable[[BaseException], Awaitable[None]]


# ─────────────────────────── errors ───────────────────────────
class QueueError(Exception):
    """Base class for every queue rejection."""


class QueueFull(QueueError):
    """The global queue has no free slot."""


class UserLimitReached(QueueError):
    """The user already has MAX_USER_TASKS tasks in the system."""


class CooldownActive(QueueError):
    """The user must wait before using this command again."""

    def __init__(self, remaining: float):
        super().__init__(f"cooldown: {remaining:.1f}s left")
        self.remaining = remaining


# ─────────────────────────── task ───────────────────────────
@dataclass
class Task:
    user_id: int
    kind: str
    job: Job
    timeout: float
    on_start: Optional[Hook] = None
    on_timeout: Optional[Hook] = None
    on_error: Optional[ErrorHook] = None
    created: float = field(default_factory=time.monotonic)


async def _call(hook: Optional[Hook]) -> None:
    """Run an optional hook; a failing hook must never break the worker."""
    if hook is None:
        return
    try:
        await hook()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("queue hook failed", exc_info=True)


# ─────────────────────────── queue ───────────────────────────
class TaskQueue:
    def __init__(self, max_running: int, max_waiting: int, max_user_tasks: int):
        self.max_running = max(1, int(max_running))
        self.max_waiting = max(0, int(max_waiting))
        self.max_user_tasks = max(1, int(max_user_tasks))

        self._queues: dict[int, deque] = {}     # user_id -> waiting tasks
        self._ready: deque = deque()            # users that have waiting tasks
        self._busy: set = set()                 # users with a running task
        self._load: dict[int, int] = {}         # user_id -> running + waiting
        self._cooldowns: dict[tuple, float] = {}  # (user_id, kind) -> expiry

        self._running = 0
        self._waiting = 0
        self.finished = 0
        self.failed = 0

        self._wake = asyncio.Event()
        self._workers: list = []

    # ───────────── public API ─────────────
    def cooldown_left(self, user_id: int, kind: str) -> float:
        left = self._cooldowns.get((user_id, kind), 0) - time.monotonic()
        return left if left > 0 else 0.0

    def user_load(self, user_id: int) -> int:
        return self._load.get(user_id, 0)

    def snapshot(self) -> dict:
        capacity = self.max_running + self.max_waiting
        in_system = self._running + self._waiting
        return {
            "running": self._running,
            "waiting": self._waiting,
            "in_system": in_system,
            "capacity": capacity,
            "free": max(0, capacity - in_system),
            "max_running": self.max_running,
            "max_waiting": self.max_waiting,
            "finished": self.finished,
            "failed": self.failed,
            "active_users": len(self._load),
        }

    def submit(
        self,
        user_id: int,
        job: Job,
        *,
        kind: str = "task",
        timeout: Optional[float] = None,
        cooldown: float = 0,
        on_start: Optional[Hook] = None,
        on_timeout: Optional[Hook] = None,
        on_error: Optional[ErrorHook] = None,
    ) -> int:
        """Add a job. Returns its position (0 = starts right away).

        Must be called from inside the running event loop. Raises
        ``CooldownActive``, ``UserLimitReached`` or ``QueueFull``.
        """
        now = time.monotonic()

        if cooldown > 0:
            left = self.cooldown_left(user_id, kind)
            if left > 0:
                raise CooldownActive(left)

        if self._load.get(user_id, 0) >= self.max_user_tasks:
            raise UserLimitReached()

        if self._running + self._waiting >= self.max_running + self.max_waiting:
            raise QueueFull()

        self._ensure_workers()

        task = Task(
            user_id=user_id,
            kind=kind,
            job=job,
            timeout=float(timeout or QUEUE_TASK_TIMEOUT),
            on_start=on_start,
            on_timeout=on_timeout,
            on_error=on_error,
        )

        user_has_tasks = self._load.get(user_id, 0) > 0
        # Users ahead of us that will each grab a free worker.
        runnable_ahead = sum(
            1 for uid in self._ready if uid != user_id and uid not in self._busy
        )

        user_queue = self._queues.get(user_id)
        if user_queue is None:
            user_queue = self._queues[user_id] = deque()
            self._ready.append(user_id)
        user_queue.append(task)

        self._waiting += 1
        self._load[user_id] = self._load.get(user_id, 0) + 1

        if cooldown > 0:
            self._cooldowns[(user_id, kind)] = now + cooldown
            self._prune_cooldowns(now)

        free_workers = max(0, self.max_running - self._running)
        position = max(0, runnable_ahead + 1 - free_workers)
        if user_has_tasks:
            # Their own earlier task has to finish first.
            position = max(position, 1)

        self._wake.set()
        return position

    # ───────────── internals ─────────────
    def _prune_cooldowns(self, now: float) -> None:
        if len(self._cooldowns) < 2000:
            return
        for key, expiry in list(self._cooldowns.items()):
            if expiry <= now:
                self._cooldowns.pop(key, None)

    def _ensure_workers(self) -> None:
        self._workers = [w for w in self._workers if not w.done()]
        loop = asyncio.get_running_loop()
        while len(self._workers) < self.max_running:
            self._workers.append(loop.create_task(self._worker()))

    def _take(self) -> Optional[Task]:
        """Pick the next job round-robin, skipping users who are busy."""
        for _ in range(len(self._ready)):
            user_id = self._ready.popleft()
            user_queue = self._queues.get(user_id)

            if not user_queue:  # defensive: should never happen
                self._queues.pop(user_id, None)
                continue

            if user_id in self._busy:
                self._ready.append(user_id)
                continue

            task = user_queue.popleft()
            if user_queue:
                self._ready.append(user_id)
            else:
                del self._queues[user_id]

            self._waiting -= 1
            self._running += 1
            self._busy.add(user_id)
            return task
        return None

    async def _worker(self) -> None:
        while True:
            try:
                task = self._take()
                if task is None:
                    # No await between _take() and clear(): no lost wake-ups.
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                await self._execute(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("queue worker error")
                await asyncio.sleep(1)

    async def _execute(self, task: Task) -> None:
        succeeded = False
        try:
            await _call(task.on_start)
            await asyncio.wait_for(task.job(), timeout=task.timeout)
            succeeded = True

        except asyncio.TimeoutError:
            log.warning("job timed out: kind=%s user=%s", task.kind, task.user_id)
            await _call(task.on_timeout)

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            log.exception("job failed: kind=%s user=%s", task.kind, task.user_id)
            if task.on_error is not None:
                try:
                    await task.on_error(exc)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("on_error hook failed", exc_info=True)

        finally:
            self._running -= 1
            self._busy.discard(task.user_id)

            remaining = self._load.get(task.user_id, 1) - 1
            if remaining > 0:
                self._load[task.user_id] = remaining
            else:
                self._load.pop(task.user_id, None)

            if succeeded:
                self.finished += 1
            else:
                self.failed += 1

            self._wake.set()


task_queue = TaskQueue(MAX_RUNNING, MAX_WAITING, MAX_USER_TASKS)


# ─────────────────────── helper used by the handlers ───────────────────────
async def _safe_reply(message, text: str):
    try:
        return await message.reply_text(text)
    except Exception:
        return None


async def _safe_delete(message) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except Exception:
        pass


async def enqueue(
    message,
    user_id: int,
    job: Job,
    *,
    kind: str,
    command: str,
    timeout: Optional[float] = None,
    cooldown: float = 0,
    error_text: str = "❌ Something went wrong. Please try again later.",
    timeout_text: str = "⏳ This took too long and was stopped. Please try again.",
) -> bool:
    """Queue ``job`` and talk to the user.

    * Rejections (cooldown / user limit / full queue) are answered with a
      friendly reply and ``False`` is returned.
    * If the job has to wait, a "position" notice is sent and removed
      automatically when the job starts.
    * Timeouts and unexpected errors send ``timeout_text`` / ``error_text``.
    """
    state = {"notice": None, "started": False}

    async def on_start():
        state["started"] = True
        notice, state["notice"] = state["notice"], None
        await _safe_delete(notice)

    async def on_timeout():
        await _safe_reply(message, timeout_text)

    async def on_error(_exc):
        await _safe_reply(message, error_text)

    try:
        position = task_queue.submit(
            user_id,
            job,
            kind=kind,
            timeout=timeout,
            cooldown=cooldown,
            on_start=on_start,
            on_timeout=on_timeout,
            on_error=on_error,
        )
    except CooldownActive as exc:
        seconds = int(exc.remaining) + 1
        await _safe_reply(
            message, f"⏳ Please wait {seconds} second(s) before using {command} again."
        )
        return False
    except UserLimitReached:
        await _safe_reply(
            message,
            "⏳ You already have a task in progress. Please wait until it finishes.",
        )
        return False
    except QueueFull:
        snap = task_queue.snapshot()
        await _safe_reply(
            message,
            f"🚦 The queue is full ({snap['in_system']}/{snap['capacity']}). "
            "Please try again in a little while.",
        )
        return False
    except Exception:
        log.exception("could not enqueue job")
        await _safe_reply(message, "❌ Couldn't add your request to the queue.")
        return False

    if position > 0:
        notice = await _safe_reply(
            message,
            f"📋 Added to the queue — position #{position}.\n"
            "I'll start as soon as it's your turn.",
        )
        if state["started"]:
            # The job began while the notice was being sent.
            await _safe_delete(notice)
        else:
            state["notice"] = notice

    return True
