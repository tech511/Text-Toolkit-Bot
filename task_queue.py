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

Owner (``OWNER_ID`` in config.py)
---------------------------------
The owner has NO restrictions: no cooldown, no per-user task limit, no
global capacity limit, and the owner's jobs start immediately, in parallel,
outside the worker pool (so they never wait behind other users and never
take a slot away from them). Only the per-job timeout still applies, so a
stuck job can't hang forever.

Cooldown
--------
``cooldown_on_success=True`` starts the cooldown only when the job finished
successfully. A job that fails, times out, is cancelled, or returns
``False`` ("finished, but nothing was delivered") gives NO cooldown.

Cancelling
----------
``task_queue.cancel(task)`` removes a waiting job or stops a running one;
``task_queue.cancel_user(user_id, kind)`` does it for all of a user's jobs.
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

try:  # OWNER_ID may be missing in very old config files
    from config import OWNER_ID
except ImportError:  # pragma: no cover
    OWNER_ID = 0

log = logging.getLogger("task_queue")

# A job may return ``False`` to say "I finished, but nothing was delivered"
# (for example: no speech could be made). That is not a success, so it never
# starts a cooldown. Returning ``None`` / ``True`` counts as success.
Job = Callable[[], Awaitable[Optional[bool]]]
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
# eq=False -> tasks are compared / hashed by identity (needed for sets).
@dataclass(eq=False)
class Task:
    user_id: int
    kind: str
    job: Job
    timeout: float
    on_start: Optional[Hook] = None
    on_timeout: Optional[Hook] = None
    on_error: Optional[ErrorHook] = None
    on_cancel: Optional[Hook] = None
    cooldown: float = 0
    cooldown_on_success: bool = False
    owner: bool = False
    created: float = field(default_factory=time.monotonic)

    # runtime state (managed by the queue)
    state: str = "waiting"          # waiting | running | done
    cancelled: bool = False
    outcome: Optional[str] = None   # ok | declined | timeout | error | cancelled
    runner: Optional["asyncio.Future"] = None


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
    def __init__(
        self,
        max_running: int,
        max_waiting: int,
        max_user_tasks: int,
        owner_id: int = 0,
    ):
        self.max_running = max(1, int(max_running))
        self.max_waiting = max(0, int(max_waiting))
        self.max_user_tasks = max(1, int(max_user_tasks))
        self.owner_id = int(owner_id or 0)

        self._queues: dict[int, deque] = {}     # user_id -> waiting tasks
        self._ready: deque = deque()            # users that have waiting tasks
        self._busy: set = set()                 # users with a running task
        self._load: dict[int, int] = {}         # user_id -> running + waiting
        self._live: dict[int, set] = {}         # user_id -> every live task
        self._cooldowns: dict[tuple, float] = {}  # (user_id, kind) -> expiry

        self._running = 0
        self._waiting = 0
        self._owner_running = 0
        self.finished = 0
        self.failed = 0
        self.cancelled = 0

        self._wake = asyncio.Event()
        self._workers: list = []
        self._owner_jobs: set = set()           # keeps owner jobs referenced

    # ───────────── public API ─────────────
    def is_owner(self, user_id: int) -> bool:
        return bool(self.owner_id) and user_id == self.owner_id

    def cooldown_left(self, user_id: int, kind: str) -> float:
        if self.is_owner(user_id):
            return 0.0
        left = self._cooldowns.get((user_id, kind), 0) - time.monotonic()
        return left if left > 0 else 0.0

    def user_load(self, user_id: int) -> int:
        """Running + waiting jobs of a user (the owner is never limited)."""
        if self.is_owner(user_id):
            return 0
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
            "cancelled": self.cancelled,
            "owner_running": self._owner_running,
            "active_users": len(self._load),
        }

    def submit_task(
        self,
        user_id: int,
        job: Job,
        *,
        kind: str = "task",
        timeout: Optional[float] = None,
        cooldown: float = 0,
        cooldown_on_success: bool = False,
        on_start: Optional[Hook] = None,
        on_timeout: Optional[Hook] = None,
        on_error: Optional[ErrorHook] = None,
        on_cancel: Optional[Hook] = None,
    ) -> tuple:
        """Add a job. Returns ``(task, position)`` (position 0 = starts now).

        Must be called from inside the running event loop. Raises
        ``CooldownActive``, ``UserLimitReached`` or ``QueueFull`` — never for
        the owner.
        """
        now = time.monotonic()
        owner = self.is_owner(user_id)

        if not owner:
            if cooldown > 0:
                left = self.cooldown_left(user_id, kind)
                if left > 0:
                    raise CooldownActive(left)

            if self._load.get(user_id, 0) >= self.max_user_tasks:
                raise UserLimitReached()

            if self._running + self._waiting >= self.max_running + self.max_waiting:
                raise QueueFull()

        task = Task(
            user_id=user_id,
            kind=kind,
            job=job,
            timeout=float(timeout or QUEUE_TASK_TIMEOUT),
            on_start=on_start,
            on_timeout=on_timeout,
            on_error=on_error,
            on_cancel=on_cancel,
            cooldown=0 if owner else float(cooldown or 0),
            cooldown_on_success=bool(cooldown_on_success),
            owner=owner,
        )

        loop = asyncio.get_running_loop()
        self._live.setdefault(user_id, set()).add(task)

        # ── owner: start immediately, outside the worker pool ──
        if owner:
            self._owner_running += 1
            task.state = "running"
            runner = loop.create_task(self._execute(task))
            self._owner_jobs.add(runner)
            runner.add_done_callback(self._owner_jobs.discard)
            return task, 0

        # ── everybody else: fair per-user queue ──
        self._ensure_workers()

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

        if cooldown > 0 and not cooldown_on_success:
            self._cooldowns[(user_id, kind)] = now + cooldown
            self._prune_cooldowns(now)

        free_workers = max(0, self.max_running - self._running)
        position = max(0, runnable_ahead + 1 - free_workers)
        if user_has_tasks:
            # Their own earlier task has to finish first.
            position = max(position, 1)

        self._wake.set()
        return task, position

    def submit(self, user_id: int, job: Job, **kwargs) -> int:
        """Same as ``submit_task`` but only returns the position."""
        return self.submit_task(user_id, job, **kwargs)[1]

    async def cancel(self, task: Optional[Task]) -> str:
        """Cancel one job.

        Returns ``"waiting"`` (removed from the queue), ``"running"``
        (being stopped) or ``"gone"`` (already finished / cancelled).
        A cancelled job never starts a cooldown.
        """
        if task is None or task.state == "done" or task.cancelled:
            return "gone"

        task.cancelled = True

        if task.state == "waiting":
            self._remove_waiting(task)
            task.state = "done"
            task.outcome = "cancelled"
            self.cancelled += 1
            self._forget(task)
            self._wake.set()
            await _call(task.on_cancel)
            return "waiting"

        # running: cancel the job; _execute() does the clean-up.
        runner = task.runner
        if runner is not None and not runner.done():
            runner.cancel()
        return "running"

    async def cancel_user(self, user_id: int, kind: Optional[str] = None) -> int:
        """Cancel every live job of a user (optionally only one kind)."""
        count = 0
        for task in list(self._live.get(user_id, ())):
            if kind is not None and task.kind != kind:
                continue
            if await self.cancel(task) != "gone":
                count += 1
        return count

    # ───────────── internals ─────────────
    def _prune_cooldowns(self, now: float) -> None:
        if len(self._cooldowns) < 2000:
            return
        for key, expiry in list(self._cooldowns.items()):
            if expiry <= now:
                self._cooldowns.pop(key, None)

    def _forget(self, task: Task) -> None:
        """Drop a finished/cancelled task from the live registry."""
        live = self._live.get(task.user_id)
        if live is not None:
            live.discard(task)
            if not live:
                self._live.pop(task.user_id, None)

    def _remove_waiting(self, task: Task) -> None:
        uid = task.user_id
        user_queue = self._queues.get(uid)
        if user_queue is not None:
            try:
                user_queue.remove(task)
            except ValueError:
                pass
            if not user_queue:
                del self._queues[uid]
                try:
                    self._ready.remove(uid)
                except ValueError:
                    pass
        self._waiting = max(0, self._waiting - 1)
        self._release_load(uid)

    def _release_load(self, uid: int) -> None:
        remaining = self._load.get(uid, 1) - 1
        if remaining > 0:
            self._load[uid] = remaining
        else:
            self._load.pop(uid, None)

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
            task.state = "running"
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
        """Run one job (for the worker pool and for the owner alike)."""
        outcome = "error"
        runner = None
        try:
            await _call(task.on_start)

            if task.cancelled:  # cancelled while on_start was running
                outcome = "cancelled"
                done = None
            else:
                runner = asyncio.ensure_future(task.job())
                task.runner = runner
                done, _ = await asyncio.wait({runner}, timeout=task.timeout)

            if done is None:
                pass

            elif not done:
                # Hard timeout: stop the job and wait for its clean-up.
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
                log.warning("job timed out: kind=%s user=%s", task.kind, task.user_id)
                outcome = "timeout"
                await _call(task.on_timeout)

            elif runner.cancelled():
                outcome = "cancelled"

            else:
                exc = runner.exception()
                if exc is None:
                    outcome = "declined" if runner.result() is False else "ok"
                elif isinstance(exc, asyncio.TimeoutError):
                    log.warning(
                        "job timed out: kind=%s user=%s", task.kind, task.user_id
                    )
                    outcome = "timeout"
                    await _call(task.on_timeout)
                else:
                    log.error(
                        "job failed: kind=%s user=%s",
                        task.kind,
                        task.user_id,
                        exc_info=exc,
                    )
                    outcome = "error"
                    if task.on_error is not None:
                        try:
                            await task.on_error(exc)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            log.warning("on_error hook failed", exc_info=True)

        except asyncio.CancelledError:
            # The worker itself is being shut down.
            outcome = "cancelled"
            if runner is not None and not runner.done():
                runner.cancel()
            raise

        finally:
            task.state = "done"
            task.outcome = outcome
            task.runner = None

            if task.owner:
                self._owner_running = max(0, self._owner_running - 1)
            else:
                self._running = max(0, self._running - 1)
                self._busy.discard(task.user_id)
                self._release_load(task.user_id)

            self._forget(task)

            if outcome == "ok":
                self.finished += 1
                if (
                    task.cooldown > 0
                    and task.cooldown_on_success
                    and not task.owner
                ):
                    now = time.monotonic()
                    self._cooldowns[(task.user_id, task.kind)] = now + task.cooldown
                    self._prune_cooldowns(now)
            elif outcome == "cancelled":
                self.cancelled += 1
            else:
                self.failed += 1

            self._wake.set()

        if outcome == "cancelled":
            await _call(task.on_cancel)


task_queue = TaskQueue(MAX_RUNNING, MAX_WAITING, MAX_USER_TASKS, OWNER_ID)


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


@dataclass
class EnqueueResult:
    """Result of ``enqueue``. It is truthy when the job was accepted, so the
    old ``if accepted:`` style still works."""

    accepted: bool
    task: Optional[Task] = None
    position: int = 0

    def __bool__(self) -> bool:
        return self.accepted


async def enqueue(
    message,
    user_id: int,
    job: Job,
    *,
    kind: str,
    command: str,
    timeout: Optional[float] = None,
    cooldown: float = 0,
    cooldown_on_success: bool = False,
    show_position: bool = True,
    on_cancel: Optional[Hook] = None,
    error_text: str = "❌ Something went wrong. Please try again later.",
    timeout_text: str = "⏳ This took too long and was stopped. Please try again.",
) -> EnqueueResult:
    """Queue ``job`` and talk to the user.

    * Rejections (cooldown / user limit / full queue) are answered with a
      friendly reply and a falsy result is returned.
    * If the job has to wait and ``show_position`` is true, a "position"
      notice is sent and removed automatically when the job starts.
    * Timeouts and unexpected errors send ``timeout_text`` / ``error_text``.
    * ``cooldown_on_success=True`` -> the cooldown only starts after the job
      finished successfully.
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

    async def on_cancelled():
        notice, state["notice"] = state["notice"], None
        await _safe_delete(notice)
        if on_cancel is not None:
            await on_cancel()

    try:
        task, position = task_queue.submit_task(
            user_id,
            job,
            kind=kind,
            timeout=timeout,
            cooldown=cooldown,
            cooldown_on_success=cooldown_on_success,
            on_start=on_start,
            on_timeout=on_timeout,
            on_error=on_error,
            on_cancel=on_cancelled,
        )
    except CooldownActive as exc:
        seconds = int(exc.remaining) + 1
        await _safe_reply(
            message, f"⏳ Please wait {seconds} second(s) before using {command} again."
        )
        return EnqueueResult(False)
    except UserLimitReached:
        await _safe_reply(
            message,
            "⏳ You already have a task in progress. Please wait until it finishes.",
        )
        return EnqueueResult(False)
    except QueueFull:
        snap = task_queue.snapshot()
        await _safe_reply(
            message,
            f"🚦 The queue is full ({snap['in_system']}/{snap['capacity']}). "
            "Please try again in a little while.",
        )
        return EnqueueResult(False)
    except Exception:
        log.exception("could not enqueue job")
        await _safe_reply(message, "❌ Couldn't add your request to the queue.")
        return EnqueueResult(False)

    if position > 0 and show_position:
        notice = await _safe_reply(
            message,
            f"📋 Added to the queue — position #{position}.\n"
            "I'll start as soon as it's your turn.",
        )
        if state["started"] or task.state != "waiting":
            # The job began (or was cancelled) while the notice was being sent.
            await _safe_delete(notice)
        else:
            state["notice"] = notice

    return EnqueueResult(True, task, position)
