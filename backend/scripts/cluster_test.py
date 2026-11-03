"""Two-worker cluster probe: proves a room behaves identically across processes.

Start two workers that share one database and one bus, then run this against both
addresses. It checks the things that used to be impossible with in-memory state:

  1. a client on worker B receives events produced on worker A (bus fan-out)
  2. a command sent to worker B is executed by the room's owner on worker A
  3. both workers report the same queue order
  4. REST reads on the non-owning worker are answered by the owner (HTTP forwarding)
  5. presence includes sockets from every worker
  6. a late joiner on worker B gets the full current state

Usage:
    python backend/scripts/cluster_test.py --a http://127.0.0.1:8142 --b http://127.0.0.1:8143
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import websockets

HOST = ("host@aidj.dev", "Host123!")
LISTENER_A = ("listener@aidj.dev", "Listener123!")
LISTENER_B = ("listener2@aidj.dev", "Listener123!")
LATE = ("listener3@aidj.dev", "Listener123!")


def auth(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['accessToken']}"}


async def login(client: httpx.AsyncClient, credentials: tuple[str, str]) -> dict:
    response = await client.post(
        "/api/v1/auth/login", json={"email": credentials[0], "password": credentials[1]}
    )
    response.raise_for_status()
    return response.json()


def ws_url(base: str, lounge_id: str, session: dict) -> str:
    base = base.replace("https://", "wss://").replace("http://", "ws://")
    return f"{base}/ws/lounges/{lounge_id}?token={session['accessToken']}"


async def read_event(ws, name: str, timeout: float = 10.0) -> dict:
    deadline = time.perf_counter() + timeout
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError(f"never received {name}")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        message = json.loads(raw)
        if message.get("event") == name:
            return message


async def connect(base: str, lounge_id: str, session: dict) -> tuple[object, dict]:
    ws = await websockets.connect(ws_url(base, lounge_id, session), max_size=2**22)
    state = await read_event(ws, "lounge.state")
    return ws, state["payload"]


async def main(a_url: str, b_url: str) -> int:
    report: dict = {}
    checks: dict[str, bool] = {}

    async with httpx.AsyncClient(base_url=a_url, timeout=20) as ca, httpx.AsyncClient(
        base_url=b_url, timeout=20
    ) as cb:
        host = await login(ca, HOST)
        listener_a = await login(ca, LISTENER_A)
        listener_b = await login(cb, LISTENER_B)
        late = await login(cb, LATE)

        health_a = (await ca.get("/api/v1/health")).json()
        health_b = (await cb.get("/api/v1/health")).json()
        report["workers"] = {"a": health_a, "b": health_b}
        checks["two_distinct_workers"] = health_a.get("worker") != health_b.get("worker")

        created = await ca.post(
            "/api/v1/lounges", headers=auth(host), json={"name": "Cluster Probe"}
        )
        created.raise_for_status()
        lounge_id = created.json()["loungeId"]
        report["loungeId"] = lounge_id

        host_ws, host_state = await connect(a_url, lounge_id, host)
        ws_a, _ = await connect(a_url, lounge_id, listener_a)
        ws_b, state_b = await connect(b_url, lounge_id, listener_b)

        # ---- 5. presence spans workers ------------------------------------
        last_state = state_b
        for _ in range(20):
            if len(last_state.get("participants") or []) >= 3:
                break
            await asyncio.sleep(0.3)
            snapshot = (await ca.get(f"/api/v1/lounges/{lounge_id}/state", headers=auth(host))).json()
            last_state = snapshot
        participants = {p["userId"] for p in last_state.get("participants") or []}
        report["participants"] = len(participants)
        checks["presence_spans_workers"] = len(participants) >= 3

        queue = host_state.get("queue") or []
        if not queue:
            raise RuntimeError("seeded lounge has no queue entries to vote on")

        # ---- 1/2. cross-worker command + fan-out --------------------------
        entry_id = queue[0]["entryId"]
        for ws in (host_ws, ws_a, ws_b):
            try:
                while True:
                    await asyncio.wait_for(ws.recv(), timeout=0.01)
            except (asyncio.TimeoutError, TimeoutError):
                pass

        fired = time.perf_counter()
        await ws_b.send(
            json.dumps({"event": "lounge.queue.vote", "payload": {"entryId": entry_id}})
        )
        try:
            cross = await read_event(host_ws, "lounge.queue.update", timeout=10)
            cross_ms = (time.perf_counter() - fired) * 1000
            checks["command_from_edge_worker"] = True
            checks["event_reaches_other_worker"] = True
        except TimeoutError:
            cross, cross_ms = None, -1.0
            checks["command_from_edge_worker"] = False
            checks["event_reaches_other_worker"] = False
        report["crossWorkerCommandMs"] = round(cross_ms, 2)

        # ---- 3. identical queue order on both workers --------------------
        order_a = [item["entryId"] for item in (cross or {}).get("payload", {}).get("queue", [])]
        frames_b = []
        try:
            frame_b = await read_event(ws_b, "lounge.queue.update", timeout=5)
            frames_b.append(frame_b)
        except TimeoutError:
            pass
        order_b = [item["entryId"] for item in (frames_b[0]["payload"]["queue"] if frames_b else [])]
        report["queueOrderA"] = order_a
        report["queueOrderB"] = order_b
        checks["same_queue_order"] = bool(order_a) and order_a == order_b

        # ---- 4. REST read on the non-owning worker -----------------------
        started = time.perf_counter()
        proxied = await cb.get(f"/api/v1/lounges/{lounge_id}/state", headers=auth(host))
        proxied_ms = (time.perf_counter() - started) * 1000
        direct = await ca.get(f"/api/v1/lounges/{lounge_id}/state", headers=auth(host))
        report["restForwardMs"] = round(proxied_ms, 2)
        checks["rest_forwarded_ok"] = proxied.status_code == 200
        if proxied.status_code == 200 and direct.status_code == 200:
            proxied_queue = [i["entryId"] for i in proxied.json().get("queue") or []]
            direct_queue = [i["entryId"] for i in direct.json().get("queue") or []]
            checks["rest_same_state"] = proxied_queue == direct_queue
        else:
            checks["rest_same_state"] = False

        # ---- chat written through the edge worker ------------------------
        chat_started = time.perf_counter()
        chat_response = await cb.post(
            f"/api/v1/lounges/{lounge_id}/chat",
            headers=auth(listener_b),
            json={"body": "sending this through worker B 🎧"},
        )
        checks["chat_post_ok"] = chat_response.status_code in (200, 201)
        try:
            chat_frame = await read_event(host_ws, "lounge.chat.message", timeout=5)
            checks["chat_broadcast_to_owner"] = chat_frame["payload"].get("userId") == listener_b["user"]["userId"]
        except TimeoutError:
            checks["chat_broadcast_to_owner"] = False
        report["chatViaEdgeMs"] = round((time.perf_counter() - chat_started) * 1000, 2)

        # ---- permission errors keep their meaning across workers ---------
        await ws_b.send(
            json.dumps({"event": "lounge.queue.promote", "payload": {"entryId": entry_id}})
        )
        try:
            error_frame = await read_event(ws_b, "lounge.error", timeout=5)
            checks["edge_permission_error"] = error_frame["payload"].get("code") == "forbidden"
        except TimeoutError:
            checks["edge_permission_error"] = False

        # ---- 6. late joiner on the edge worker ---------------------------
        join_started = time.perf_counter()
        late_ws, late_state = await connect(b_url, lounge_id, late)
        report["lateJoinOnEdgeMs"] = round((time.perf_counter() - join_started) * 1000, 2)
        checks["late_join_state"] = (report["lateJoinOnEdgeMs"] < 2000) and bool(late_state.get("queue")) is not None
        checks["late_join_sees_queue"] = len(late_state.get("queue") or []) > 0

        for ws in (late_ws, ws_b, ws_a, host_ws):
            await ws.close()

        ended = await ca.post(f"/api/v1/lounges/{lounge_id}/end", headers=auth(host))
        checks["end_lounge_ok"] = ended.status_code == 200

    report["checks"] = checks
    report["allPassed"] = all(checks.values())
    print(json.dumps(report, indent=2))

    out = Path(__file__).resolve().parents[1] / "docs" / "cluster-test-results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwritten {out}")
    return 0 if report["allPassed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--a", default="http://127.0.0.1:8142")
    parser.add_argument("--b", default="http://127.0.0.1:8143")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(main(args.a, args.b)))
    except KeyboardInterrupt:
        sys.exit(130)
