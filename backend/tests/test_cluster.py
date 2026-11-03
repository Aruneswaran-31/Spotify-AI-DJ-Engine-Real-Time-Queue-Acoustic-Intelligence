"""Cross-process plumbing tests.

These run two independent bus/lease instances against one temporary SQLite file, which
is exactly the arrangement two uvicorn workers use on a single host. They cover the
pieces that must hold for a room to behave identically no matter which worker a client
lands on: fan-out, single-writer ownership, RPC error propagation and socket routing.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.cluster import MemoryBus, Rpc, RpcTimeout, SqliteBus, SqliteLeaseStore
from app.realtime import Connection, Hub


def run(coro):
    return asyncio.run(coro)


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


# --------------------------------------------------------------------------- bus
def test_memory_bus_delivers_in_process(tmp_path):
    async def scenario():
        bus = MemoryBus()
        received: list[dict] = []

        async def handler(message):
            received.append(message)

        await bus.start(handler)
        await bus.publish("room:lg1", "event", {"hello": "world"})
        return received

    received = run(scenario())
    assert len(received) == 1
    assert received[0]["payload"] == {"hello": "world"}


def test_sqlite_bus_reaches_a_second_worker(tmp_path):
    path = str(tmp_path / "bus.db")

    async def scenario():
        worker_a = SqliteBus(path, interval_ms=5)
        worker_b = SqliteBus(path, interval_ms=5)
        seen_a: list[dict] = []
        seen_b: list[dict] = []

        async def on_a(message):
            seen_a.append(message)

        async def on_b(message):
            seen_b.append(message)

        await worker_a.start(on_a)
        await worker_b.start(on_b)
        await worker_a.publish("room:lg1", "event", {"queue": [1, 2, 3]})
        # Both the publisher and the other worker must observe the message.
        for _ in range(200):
            if seen_a and seen_b:
                break
            await asyncio.sleep(0.01)
        await worker_a.stop()
        await worker_b.stop()
        return seen_a, seen_b

    seen_a, seen_b = run(scenario())
    assert seen_a, "publishing worker never saw its own event"
    assert seen_b, "second worker never saw the event"
    assert seen_b[0]["payload"] == {"queue": [1, 2, 3]}


def test_sqlite_bus_ignores_history_before_start(tmp_path):
    path = str(tmp_path / "bus.db")

    async def scenario():
        first = SqliteBus(path, interval_ms=5)
        await first.start(lambda message: asyncio.sleep(0))

        async def noop(message):
            return None

        await first.publish("room:lg1", "event", {"stale": True})
        await asyncio.sleep(0.1)
        await first.stop()

        late = SqliteBus(path, interval_ms=5)
        seen: list[dict] = []

        async def on_late(message):
            seen.append(message)

        await late.start(on_late)
        await asyncio.sleep(0.15)
        await late.stop()
        return seen

    # A worker joining later must not replay messages published before it attached.
    assert run(scenario()) == []


# ------------------------------------------------------------------------- lease
def test_lease_grants_a_single_owner(tmp_path):
    path = str(tmp_path / "lease.db")

    async def scenario():
        store = SqliteLeaseStore(path, ttl_ms=5000)
        return (
            await store.claim("lg1", "worker-a"),
            await store.claim("lg1", "worker-b"),
            await store.owner("lg1"),
        )

    first, second, owner = run(scenario())
    assert first is True
    assert second is False
    assert owner == "worker-a"


def test_lease_is_taken_over_after_the_owner_dies(tmp_path):
    path = str(tmp_path / "lease.db")

    async def scenario():
        store = SqliteLeaseStore(path, ttl_ms=150)
        assert await store.claim("lg1", "worker-a") is True
        # A second worker cannot take a live lease...
        assert await store.claim("lg1", "worker-b") is False
        await asyncio.sleep(0.3)
        # ...but can once the holder stops renewing.
        took_over = await store.claim("lg1", "worker-b")
        return took_over, await store.owner("lg1")

    took_over, owner = run(scenario())
    assert took_over is True
    assert owner == "worker-b"


def test_lease_release_frees_the_room(tmp_path):
    path = str(tmp_path / "lease.db")

    async def scenario():
        store = SqliteLeaseStore(path, ttl_ms=5000)
        await store.claim("lg1", "worker-a")
        await store.release("lg1", "worker-a")
        return await store.claim("lg1", "worker-b")

    assert run(scenario()) is True


# --------------------------------------------------------------------------- rpc
def test_rpc_round_trips_across_workers(tmp_path):
    path = str(tmp_path / "rpc.db")

    async def scenario():
        worker_a = SqliteBus(path, interval_ms=5)
        worker_b = SqliteBus(path, interval_ms=5)
        rpc_a = Rpc(worker_a)
        rpc_b = Rpc(worker_b)

        async def on_a(message):
            rpc_a.handle(message)

        async def on_b(message):
            if rpc_b.handle(message):
                return
            if message["kind"] == "echo":
                await rpc_b.reply(
                    message["channel"], message["payload"]["reqId"], result=message["payload"]["value"]
                )
            elif message["kind"] == "boom":
                await rpc_b.reply(
                    message["channel"], message["payload"]["reqId"], error=PermissionError("not your queue")
                )

        await worker_a.start(on_a)
        await worker_b.start(on_b)
        try:
            echoed = await rpc_a.request("room:lg1", "echo", {"value": {"trackId": "t1"}}, timeout=5)
            with pytest.raises(PermissionError):
                await rpc_a.request("room:lg1", "boom", {}, timeout=5)
            return echoed
        finally:
            await worker_a.stop()
            await worker_b.stop()

    assert run(scenario()) == {"trackId": "t1"}


def test_rpc_times_out_when_nobody_answers(tmp_path):
    path = str(tmp_path / "rpc.db")

    async def scenario():
        worker = SqliteBus(path, interval_ms=5)
        rpc = Rpc(worker)
        await worker.start(lambda message: rpc.handle(message))
        try:
            with pytest.raises(RpcTimeout):
                await rpc.request("room:lg1", "nobody-home", {}, timeout=0.3)
        finally:
            await worker.stop()

    run(scenario())


# --------------------------------------------------------------------------- hub
def test_hub_routes_events_and_direct_messages_to_local_sockets():
    async def scenario():
        hub = Hub()
        socket = FakeSocket()
        conn = Connection(
            id="c1",
            ws=socket,  # type: ignore[arg-type]
            user_id="u1",
            display_name="Sam",
            role="listener",
            lounge_id="lg1",
        )
        hub.register_local(conn)
        await hub.deliver_event("lg1", {"event": "lounge.queue.update", "payload": {"queue": []}})
        await hub.deliver_event("lg2", {"event": "other.room", "payload": {}})
        await hub.deliver_direct("c1", {"event": "lounge.sync.pong", "payload": {}})
        await hub.deliver_direct("missing", {"event": "ignored", "payload": {}})
        hub.unregister_local("c1")
        await hub.deliver_event("lg1", {"event": "after.leave", "payload": {}})
        return [message["event"] for message in socket.sent]

    events = run(scenario())
    # Room-scoped events reach only that room's sockets, plus explicit direct frames.
    assert events == ["lounge.queue.update", "lounge.sync.pong"]


def test_participant_count_falls_back_to_the_peer_report():
    hub = Hub()
    hub._peer_counts["lg1"] = 7  # a room owned by another worker
    assert hub.participant_count("lg1") == 7
    assert hub.all_connections() == 0


def test_remote_connection_never_registers_as_local():
    from app.realtime import BusSocket

    hub = Hub()
    conn = Connection(
        id="c9",
        ws=BusSocket(channel="room:lg1", target_process="other", connection_id="c9"),  # type: ignore[arg-type]
        user_id="u9",
        display_name="Rae",
        role="listener",
        lounge_id="lg1",
        remote_process="other",
    )
    assert conn.is_remote is True
    hub.register_local(conn)  # explicit registration is still just a local entry
    hub.unregister_local(conn.id)
    assert hub.all_connections() == 0
