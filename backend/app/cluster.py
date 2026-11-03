"""Cross-process coordination: bus fan-out, room ownership and command forwarding.

The in-memory room model (``realtime.Room``) is authoritative *per room*, not per
process. Exactly one worker owns a lounge's state at a time; every other worker is an
edge that holds sockets and proxies work to the owner. That keeps a single writer per
room (so no divergent replicas) while still letting any worker accept a client.

Three pieces:

* :class:`Bus` - publish/subscribe between processes. ``memory`` (single process),
  ``sqlite`` (multi-worker on one host, no extra infrastructure) or ``redis``
  (multi-host). Every consumer, including the publisher, receives the message, so
  there is exactly one delivery path whether or not the cluster is enabled.
* :class:`LeaseStore` - the ownership arbiter. A lease has a TTL and is renewed by its
  holder; if the holder dies the TTL lapses and the next worker to touch the room
  takes over and rebuilds state from the database.
* :class:`Rpc` - request/response over the bus, used to forward WebSocket events and
  lounge-scoped HTTP requests to the owning worker.

Anything here is a no-op when ``BUS_BACKEND=memory``: ``cluster.enabled`` is False,
every process owns every room, and the bus delivers in-process with no polling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sqlite3
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from .config import settings

logger = logging.getLogger("aidj.cluster")

Handler = Callable[[dict], Awaitable[None]]

ROOM_CHANNEL_PREFIX = "room:"
CLUSTER_CHANNEL = "cluster"

# Error types that may travel across a process boundary and are re-raised by type so
# callers keep their existing ``except PermissionError`` / ``except ValueError`` paths.
FORWARDABLE_ERRORS: dict[str, type[Exception]] = {
    "PermissionError": PermissionError,
    "ValueError": ValueError,
    "KeyError": KeyError,
    "RuntimeError": RuntimeError,
}


class RpcError(Exception):
    """A remote call failed with an error we can reconstruct."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class RpcTimeout(RpcError):
    def __init__(self, message: str = "The owning worker did not answer in time") -> None:
        super().__init__("TimeoutError", message)


# ---------------------------------------------------------------------------- bus
class Bus:
    """Publish/subscribe transport shared by all workers."""

    name = "base"

    def __init__(self) -> None:
        self.process_id = new_process_id()
        self._handler: Handler | None = None

    async def start(self, handler: Handler) -> None:
        self._handler = handler

    async def publish(self, channel: str, kind: str, payload: dict, sender: str | None = None) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        return None

    @staticmethod
    def envelope(channel: str, kind: str, payload: dict, sender: str) -> dict:
        return {
            "channel": channel,
            "kind": kind,
            "payload": payload,
            "sender": sender,
            "ts": int(time.time() * 1000),
        }

    async def _deliver(self, message: dict) -> None:
        if self._handler is None:
            return
        try:
            await self._handler(message)
        except Exception:  # noqa: BLE001 - one bad message must not kill the loop
            logger.exception("bus handler failed for %s", message.get("kind"))


class MemoryBus(Bus):
    """In-process bus: delivery is immediate and synchronous with ``publish``."""

    name = "memory"

    async def publish(self, channel: str, kind: str, payload: dict, sender: str | None = None) -> None:
        await self._deliver(self.envelope(channel, kind, payload, sender or self.process_id))


class SqliteBus(Bus):
    """Multi-worker bus on a shared SQLite file.

    Writers append to a log table; every process tails it with its own cursor. This
    costs one small INSERT per published message and a poll every ``interval``ms, and
    it needs no broker - which matters because the app's own persistence is SQLite, so
    a multi-worker deployment usually has nothing else running.
    """

    name = "sqlite"

    def __init__(self, path: str, interval_ms: int = 12) -> None:
        super().__init__()
        self.path = path
        self.interval = max(2, interval_ms) / 1000.0
        self._cursor = 0
        self._task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._last_prune = 0.0
        self._stopping = False
        self._conn: sqlite3.Connection | None = None
        self._conn_lock = threading.Lock()

    # -- connection -----------------------------------------------------------
    def _connection(self) -> sqlite3.Connection:
        """One long-lived connection per worker.

        Opening a fresh connection per message meant paying the WAL PRAGMA and file
        setup on every publish and every poll, which dominated cross-worker latency.
        Calls arrive on different thread-pool threads, hence ``check_same_thread`` off
        and a lock around use.
        """
        if self._conn is None:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bus_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    body TEXT NOT NULL,
                    ts INTEGER NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS ix_bus_log_id ON bus_log(id)")
            self._conn = conn
        return self._conn

    def _write(self, channel: str, kind: str, body: str, sender: str, ts: int) -> None:
        with self._conn_lock:
            self._connection().execute(
                "INSERT INTO bus_log (channel, kind, sender, body, ts) VALUES (?,?,?,?,?)",
                (channel, kind, sender, body, ts),
            )

    def _read_since(self, cursor: int) -> list[tuple[int, str, str, str, str]]:
        with self._conn_lock:
            return list(
                self._connection().execute(
                    "SELECT id, channel, kind, sender, body FROM bus_log WHERE id > ? ORDER BY id LIMIT 500",
                    (cursor,),
                )
            )

    def _max_id(self) -> int:
        with self._conn_lock:
            row = self._connection().execute("SELECT COALESCE(MAX(id), 0) FROM bus_log").fetchone()
        return int(row[0]) if row else 0

    def _prune(self, horizon_ms: int) -> None:
        with self._conn_lock:
            # Never prune rows a lagging worker may still need.
            self._connection().execute("DELETE FROM bus_log WHERE ts < ?", (horizon_ms,))

    # -- lifecycle ------------------------------------------------------------
    async def start(self, handler: Handler) -> None:
        await super().start(handler)
        # Start at the log head: history belongs to processes already running.
        self._cursor = await asyncio.to_thread(self._max_id)
        self._stopping = False
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("sqlite bus attached to %s (cursor=%d)", self.path, self._cursor)

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            self._task = None
        if self._conn is not None:
            with self._conn_lock:
                self._conn.close()
                self._conn = None

    async def publish(self, channel: str, kind: str, payload: dict, sender: str | None = None) -> None:
        ts = int(time.time() * 1000)
        async with self._write_lock:
            await asyncio.to_thread(
                self._write, channel, kind, json.dumps(payload), sender or self.process_id, ts
            )

    async def _poll_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(self.interval)
                rows = await asyncio.to_thread(self._read_since, self._cursor)
                for row_id, channel, kind, sender, body in rows:
                    self._cursor = row_id
                    try:
                        payload = json.loads(body)
                    except json.JSONDecodeError:  # pragma: no cover - defensive
                        continue
                    await self._deliver(
                        {"channel": channel, "kind": kind, "payload": payload, "sender": sender}
                    )
                now = time.time()
                if now - self._last_prune > 30 and (int(now) % 7 == 0):
                    self._last_prune = now
                    await asyncio.to_thread(self._prune, int((now - 120) * 1000))
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                return
            except Exception:  # noqa: BLE001
                logger.exception("sqlite bus poll failed")
                await asyncio.sleep(0.5)


class RedisBus(Bus):
    """Multi-host bus over Redis pub/sub (requires the ``redis`` package)."""

    name = "redis"

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url
        self._client: Any = None
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self, handler: Handler) -> None:
        await super().start(handler)
        try:
            from redis import asyncio as redis_asyncio  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "BUS_BACKEND=redis needs the 'redis' package: pip install redis"
            ) from exc
        self._client = redis_asyncio.from_url(self.url, decode_responses=True)
        self._stopping = False
        self._task = asyncio.create_task(self._listen())
        logger.info("redis bus attached to %s", self.url)

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            self._task = None
        if self._client is not None:
            await self._client.aclose()

    async def publish(self, channel: str, kind: str, payload: dict, sender: str | None = None) -> None:
        message = self.envelope(channel, kind, payload, sender or self.process_id)
        await self._client.publish("aidj", json.dumps(message))

    async def _listen(self) -> None:
        pubsub = self._client.pubsub()
        await pubsub.subscribe("aidj")
        while not self._stopping:
            try:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if not message:
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    data = data.decode()
                await self._deliver(json.loads(data))
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                return
            except Exception:  # noqa: BLE001
                logger.exception("redis bus listen failed")
                await asyncio.sleep(0.5)


# -------------------------------------------------------------------------- lease
class LeaseStore:
    """Single-writer arbiter: at most one process owns a room's live state."""

    ttl_ms = 5000

    async def claim(self, room_id: str, owner: str) -> bool:
        raise NotImplementedError

    async def owner(self, room_id: str) -> str | None:
        raise NotImplementedError

    async def renew(self, room_id: str, owner: str) -> bool:
        raise NotImplementedError

    async def release(self, room_id: str, owner: str) -> None:
        return None


class MemoryLeaseStore(LeaseStore):
    """Every room is owned by this process (single-process mode)."""

    def __init__(self) -> None:
        self._owner: dict[str, str] = {}

    async def claim(self, room_id: str, owner: str) -> bool:
        self._owner.setdefault(room_id, owner)
        return self._owner[room_id] == owner

    async def owner(self, room_id: str) -> str | None:
        return self._owner.get(room_id)

    async def renew(self, room_id: str, owner: str) -> bool:
        return self._owner.get(room_id) in (None, owner)

    async def release(self, room_id: str, owner: str) -> None:
        if self._owner.get(room_id) == owner:
            self._owner.pop(room_id, None)


class SqliteLeaseStore(LeaseStore):
    """Leases in the shared SQLite file, arbitrated by ``BEGIN IMMEDIATE``."""

    def __init__(self, path: str, ttl_ms: int = 5000) -> None:
        self.path = path
        self.ttl_ms = ttl_ms
        self._ready = False
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None
        self._conn_lock = threading.Lock()

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS room_lease (
                    room_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                )
                """
            )
            self._conn = conn
        return self._conn

    def _ensure_schema(self) -> None:
        with self._conn_lock:
            self._connection()
        self._ready = True

    async def ready(self) -> None:
        if not self._ready:
            async with self._lock:
                if not self._ready:
                    await asyncio.to_thread(self._ensure_schema)

    def _claim(self, room_id: str, owner: str, now: int) -> bool:
        with self._conn_lock:
            conn = self._connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT OR IGNORE INTO room_lease (room_id, owner, expires_at) VALUES (?,?,?)",
                    (room_id, owner, now + self.ttl_ms),
                )
                cursor = conn.execute(
                    "UPDATE room_lease SET owner=?, expires_at=? "
                    "WHERE room_id=? AND (owner=? OR expires_at < ?)",
                    (owner, now + self.ttl_ms, room_id, owner, now),
                )
                won = cursor.rowcount == 1
                conn.execute("COMMIT")
                return won
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                raise

    def _owner(self, room_id: str, now: int) -> str | None:
        with self._conn_lock:
            row = self._connection().execute(
                "SELECT owner, expires_at FROM room_lease WHERE room_id=?", (room_id,)
            ).fetchone()
        if row is None or row[1] < now:
            return None
        return row[0]

    def _renew(self, room_id: str, owner: str, now: int) -> bool:
        with self._conn_lock:
            cursor = self._connection().execute(
                "UPDATE room_lease SET expires_at=? WHERE room_id=? AND owner=?",
                (now + self.ttl_ms, room_id, owner),
            )
            return cursor.rowcount == 1

    def _release(self, room_id: str, owner: str) -> None:
        with self._conn_lock:
            self._connection().execute(
                "DELETE FROM room_lease WHERE room_id=? AND owner=?", (room_id, owner)
            )

    async def claim(self, room_id: str, owner: str) -> bool:
        await self.ready()
        async with self._lock:
            return await asyncio.to_thread(self._claim, room_id, owner, int(time.time() * 1000))

    async def owner(self, room_id: str) -> str | None:
        await self.ready()
        return await asyncio.to_thread(self._owner, room_id, int(time.time() * 1000))

    async def renew(self, room_id: str, owner: str) -> bool:
        await self.ready()
        return await asyncio.to_thread(self._renew, room_id, owner, int(time.time() * 1000))

    async def release(self, room_id: str, owner: str) -> None:
        await self.ready()
        await asyncio.to_thread(self._release, room_id, owner)


# ---------------------------------------------------------------------------- rpc
def new_process_id() -> str:
    host = socket.gethostname().split(".")[0][:24]
    return f"{host}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class Rpc:
    """Request/response over the bus, keyed by a correlation id."""

    def __init__(self, bus: Bus) -> None:
        self.bus = bus
        self._pending: dict[str, asyncio.Future] = {}

    async def request(self, channel: str, kind: str, payload: dict, timeout: float = 6.0) -> Any:
        req_id = f"rpc_{uuid.uuid4().hex[:12]}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        try:
            await self.bus.publish(channel, kind, {**payload, "reqId": req_id})
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RpcTimeout() from exc
        finally:
            self._pending.pop(req_id, None)

    def handle(self, message: dict) -> bool:
        """Resolve a pending call. Returns True when the message was a reply."""
        if message.get("kind") != "rpc.reply":
            return False
        payload = message.get("payload") or {}
        future = self._pending.get(payload.get("reqId"))
        if future is None or future.done():
            return True
        if payload.get("ok"):
            future.set_result(payload.get("result"))
        else:
            error = payload.get("error") or {}
            error_type = error.get("type", "RuntimeError")
            detail = error.get("message", "remote failure")
            # Rebuild the original exception class so callers keep their existing
            # ``except PermissionError`` / ``except ValueError`` branches across workers.
            rebuilt = FORWARDABLE_ERRORS.get(error_type)
            future.set_exception(rebuilt(detail) if rebuilt else RpcError(error_type, detail))
        return True

    async def reply(self, channel: str, req_id: str, *, result: Any = None, error: BaseException | None = None) -> None:
        if error is None:
            payload = {"reqId": req_id, "ok": True, "result": result}
        else:
            payload = {
                "reqId": req_id,
                "ok": False,
                "error": {"type": type(error).__name__, "message": str(error)},
            }
        await self.bus.publish(channel, "rpc.reply", payload)


# ------------------------------------------------------------------------ cluster
class Cluster:
    """Owns the bus, the lease store and the RPC helper."""

    def __init__(self) -> None:
        self.backend = settings.BUS_BACKEND
        self.bus: Bus = self._build_bus()
        self.process_id = self.bus.process_id
        self.leases: LeaseStore = (
            MemoryLeaseStore() if self.backend == "memory" else SqliteLeaseStore(settings.BUS_SQLITE_PATH)
        )
        self.rpc = Rpc(self.bus)
        self._owned: set[str] = set()
        self._renew_task: asyncio.Task | None = None
        self._stopping = False

    def _build_bus(self) -> Bus:
        if self.backend == "sqlite":
            return SqliteBus(settings.BUS_SQLITE_PATH, settings.BUS_POLL_MS)
        if self.backend == "redis":
            return RedisBus(settings.REDIS_URL)
        return MemoryBus()

    @property
    def enabled(self) -> bool:
        return self.backend != "memory"

    @staticmethod
    def room_channel(lounge_id: str) -> str:
        return f"{ROOM_CHANNEL_PREFIX}{lounge_id}"

    async def start(self, handler: Handler) -> None:
        await self.bus.start(handler)
        if self.enabled:
            self._stopping = False
            self._renew_task = asyncio.create_task(self._renew_loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._renew_task:
            self._renew_task.cancel()
            self._renew_task = None
        for room_id in list(self._owned):
            try:
                await self.leases.release(room_id, self.process_id)
            except Exception:  # noqa: BLE001
                logger.exception("lease release failed for %s", room_id)
        self._owned.clear()
        await self.bus.stop()

    async def claim(self, room_id: str) -> bool:
        """Become (or stay) the owner of a room. False means another worker owns it."""
        if not self.enabled:
            self._owned.add(room_id)
            return True
        if room_id in self._owned:
            return True
        won = await self.leases.claim(room_id, self.process_id)
        if won:
            self._owned.add(room_id)
        return won

    def owns(self, room_id: str) -> bool:
        return not self.enabled or room_id in self._owned

    async def remote_owner(self, room_id: str) -> str | None:
        """The owning process id when it is somebody else, else None."""
        if not self.enabled:
            return None
        owner = await self.leases.owner(room_id)
        if owner is None or owner == self.process_id:
            return None
        return owner

    async def release(self, room_id: str) -> None:
        self._owned.discard(room_id)
        if self.enabled:
            await self.leases.release(room_id, self.process_id)

    async def _renew_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(max(0.5, self.leases.ttl_ms / 3000))
                for room_id in list(self._owned):
                    try:
                        if not await self.leases.renew(room_id, self.process_id):
                            logger.warning("lost the lease on %s - another worker took over", room_id)
                            self._owned.discard(room_id)
                    except Exception:  # noqa: BLE001
                        logger.exception("lease renew failed for %s", room_id)
            except asyncio.CancelledError:  # pragma: no cover
                return
            except Exception:  # noqa: BLE001
                logger.exception("lease renew loop error")


cluster = Cluster()
