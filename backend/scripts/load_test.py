"""Scripted multi-client load test and latency measurement.

Spins up real WebSocket clients (5 seeded accounts by default, or any number via
--clients) in one lounge, fires concurrent votes, and measures:
  * queue-sync drift between clients (target < 100 ms)
  * new-participant state sync time (target < 2 s)
  * fan-out completion per room (target < 500 ms)
  * REST P95 latency (target < 200 ms)
  * AI segue P95 latency (target < 3 s)

Usage:
    python backend/scripts/load_test.py --base http://127.0.0.1:8000
    python backend/scripts/load_test.py --base http://127.0.0.1:8000 --clients 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

import httpx
import websockets

ACCOUNTS = [
    ("host@aidj.dev", "Host123!"),
    ("codj@aidj.dev", "CoDJ123!"),
    ("listener@aidj.dev", "Listener123!"),
    ("listener2@aidj.dev", "Listener123!"),
    ("listener3@aidj.dev", "Listener123!"),
]


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 2)


async def login(client: httpx.AsyncClient, email: str, password: str) -> dict:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    return response.json()


async def session_for(client: httpx.AsyncClient, email: str, password: str) -> dict:
    """Log in, registering the throwaway account the first time it is used."""
    try:
        return await login(client, email, password)
    except httpx.HTTPStatusError:
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "displayName": email.split("@")[0]},
        )
        if response.status_code not in (201, 409):
            response.raise_for_status()
        return await login(client, email, password)


async def build_sessions(client: httpx.AsyncClient, count: int) -> list[dict]:
    """Seeded demo accounts first, then deterministic synthetic listeners."""
    accounts = list(ACCOUNTS)
    while len(accounts) < count:
        index = len(accounts)
        accounts.append((f"loadtest{index}@aidj.dev", "Load123!"))
    return [await session_for(client, email, password) for email, password in accounts[:count]]


async def drain(ws, seconds: float, event: str | None = None) -> list[dict]:
    """Collect frames for a fixed window; optionally only a given event type."""
    out = []
    deadline = time.perf_counter() + seconds
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        message = json.loads(raw)
        if event is None or message.get("event") == event:
            out.append({"at": time.perf_counter(), "message": message})
    return out


async def main(base: str, client_count: int = len(ACCOUNTS)) -> int:
    async with httpx.AsyncClient(base_url=base, timeout=20) as client:
        sessions = await build_sessions(client, max(2, client_count))
        host = sessions[0]
        headers = {"Authorization": f"Bearer {host['accessToken']}"}

        tracks = (await client.get("/api/v1/tracks", headers=headers)).json()["tracks"]
        lounge = (
            await client.post(
                "/api/v1/lounges",
                headers=headers,
                json={
                    "seedTrackId": tracks[0]["trackId"],
                    "name": "Load Test Room",
                    "vibeProfile": "Peak-Time Techno",
                    "maxParticipants": max(50, len(sessions) + 5),
                    "bpmTolerancePct": 8,
                    "soundclashEnabled": True,
                    "aiVerbosity": "normal",
                },
            )
        ).json()
        lounge_id = lounge["loungeId"]
        print(f"lounge {lounge_id} created ({lounge['inviteCode']})")

        # ---- REST latency -------------------------------------------------
        rest_samples: list[float] = []
        for _ in range(30):
            started = time.perf_counter()
            response = await client.get("/api/v1/tracks", headers=headers)
            response.raise_for_status()
            rest_samples.append((time.perf_counter() - started) * 1000)

        # ---- connect + initial state -------------------------------------
        sockets = []
        state_latencies: list[float] = []
        ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
        for session in sessions:
            started = time.perf_counter()
            ws = await websockets.connect(
                f"{ws_base}/ws/lounges/{lounge_id}?token={session['accessToken']}",
                max_size=2**22,
            )
            sockets.append(ws)
            while True:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                if message["event"] == "lounge.state":
                    state_latencies.append((time.perf_counter() - started) * 1000)
                    break
        print(f"connected {len(sockets)} clients")

        # ---- concurrent votes on every queued entry ----------------------
        # Ask REST for the authoritative queue rather than replaying frames.
        queue = (await client.get(f"/api/v1/lounges/{lounge_id}/state", headers=headers)).json()["queue"]
        entries = [entry["entryId"] for entry in queue][:3]
        while len(entries) < 3:
            extra = tracks[len(entries) + 1]
            await client.post(
                f"/api/v1/lounges/{lounge_id}/queue",
                headers=headers,
                json={"trackId": extra["trackId"]},
            )
            queue = (
                await client.get(f"/api/v1/lounges/{lounge_id}/state", headers=headers)
            ).json()["queue"]
            entries = [entry["entryId"] for entry in queue][:3]

        for ws in sockets:
            await drain(ws, 0.15)

        fire_at = time.perf_counter()
        for index, ws in enumerate(sockets):
            await ws.send(
                json.dumps(
                    {
                        "event": "lounge.queue.vote",
                        "payload": {"entryId": entries[index % len(entries)]},
                    }
                )
            )
        results = await asyncio.gather(*(drain(ws, 1.2, "lounge.queue.update") for ws in sockets))

        # Every client must end on the identical order.
        final_orders = []
        receive_times = []
        for frames in results:
            if not frames:
                final_orders.append(None)
                continue
            final_orders.append([item["entryId"] for item in frames[-1]["message"]["payload"]["queue"]])
            receive_times.append(frames[-1]["at"] - fire_at)
        consistent = all(order is not None and order == final_orders[0] for order in final_orders)
        drift_ms = (max(receive_times) - min(receive_times)) * 1000 if len(receive_times) > 1 else 0.0
        storm_ms = max(receive_times) * 1000 if receive_times else 0.0

        # ---- single-event fan-out -----------------------------------------
        # The vote storm above measures throughput under concurrent writers; this
        # measures the PRD target directly: one event reaching every client.
        fire_at = time.perf_counter()
        await sockets[0].send(
            json.dumps({"event": "lounge.chat.send", "payload": {"body": "fan-out probe"}})
        )
        fanout_frames = await asyncio.gather(
            *(drain(ws, 1.5, "lounge.chat.message") for ws in sockets)
        )
        fanout_times = [frames[0]["at"] - fire_at for frames in fanout_frames if frames]
        fanout_ms = max(fanout_times) * 1000 if fanout_times else 0.0
        fanout_delivered = len(fanout_times)

        # ---- AI segue latency --------------------------------------------
        ai_samples: list[float] = []
        current = (
            await client.get(f"/api/v1/lounges/{lounge_id}/state", headers=headers)
        ).json()["nowPlaying"]["trackId"]
        candidate_ids = [
            item["trackId"]
            for item in (
                await client.get(f"/api/v1/lounges/{lounge_id}/state", headers=headers)
            ).json()["queue"]
        ]
        for _ in range(5):
            started = time.perf_counter()
            response = await client.post(
                f"/api/v1/lounges/{lounge_id}/ai/segue",
                headers=headers,
                json={
                    "currentTrackId": current,
                    "currentTs": 10,
                    "candidateTrackIds": candidate_ids,
                    "currentSentimentScore": 0.3,
                },
            )
            response.raise_for_status()
            ai_samples.append((time.perf_counter() - started) * 1000)

        # ---- late join ----------------------------------------------------
        late = await login(client, "listener3@aidj.dev", "Listener123!")
        started = time.perf_counter()
        ws = await websockets.connect(
            f"{ws_base}/ws/lounges/{lounge_id}?token={late['accessToken']}", max_size=2**22
        )
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if message["event"] == "lounge.state":
                late_join_ms = (time.perf_counter() - started) * 1000
                break
        await ws.close()

        # End the session while the host is still connected: closing the host socket
        # first would (correctly) trigger the deterministic Co-DJ host handover.
        ended = await client.post(f"/api/v1/lounges/{lounge_id}/end", headers=headers)
        if ended.status_code != 200:
            print(f"warning: could not end lounge ({ended.status_code}): {ended.text}")
        for ws in sockets:
            await ws.close()

        report = {
            "clients": len(sockets),
            "rest": {
                "samples": len(rest_samples),
                "p50Ms": round(statistics.median(rest_samples), 2),
                "p95Ms": percentile(rest_samples, 0.95),
            },
            "queueSync": {
                "consistentAcrossClients": consistent,
                "driftMs": round(drift_ms, 2),
                "fanoutCompletionMs": round(fanout_ms, 2),
                "fanoutDeliveredTo": fanout_delivered,
                "concurrentVoteStormMs": round(storm_ms, 2),
            },
            "lateJoinStateMs": round(late_join_ms, 2),
            "aiSegue": {
                "samples": len(ai_samples),
                "p50Ms": round(statistics.median(ai_samples), 2),
                "p95Ms": percentile(ai_samples, 0.95),
            },
        }
        print(json.dumps(report, indent=2))
        out = Path(__file__).resolve().parents[1] / "docs" / "load-test-results.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nwritten {out}")

        ok = (
            consistent
            and report["queueSync"]["driftMs"] < 100
            and report["rest"]["p95Ms"] < 200
            and report["aiSegue"]["p95Ms"] < 3000
            and report["lateJoinStateMs"] < 2000
            and report["queueSync"]["fanoutCompletionMs"] < 500
            and fanout_delivered == len(sockets)
        )
        print("PASS" if ok else "FAIL — one or more targets missed")
        return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--clients",
        type=int,
        default=len(ACCOUNTS),
        help="number of concurrent WebSocket clients (50 for the PRD scale check)",
    )
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(main(args.base, args.clients)))
    except KeyboardInterrupt:
        sys.exit(130)
