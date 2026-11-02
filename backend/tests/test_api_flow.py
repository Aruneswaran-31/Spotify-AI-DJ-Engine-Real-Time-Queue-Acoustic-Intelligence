"""End-to-end flows: join, live sync, Soundclash, AI DJ, analytics reconciliation."""

from __future__ import annotations

import time

from conftest import access_token, auth_headers, make_lounge


def _recv_until(ws, event: str, limit: int = 40) -> dict:
    for _ in range(limit):
        message = ws.receive_json()
        if message["event"] == event:
            return message
    raise AssertionError(f"event {event} not received")


def test_register_login_and_profile(client):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "flow@test.dev", "password": "Password123!", "displayName": "Flow User"},
    )
    assert response.status_code == 201
    token = response.json()["accessToken"]
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["displayName"] == "Flow User"

    refreshed = client.post("/api/v1/auth/refresh", json={"refreshToken": response.json()["refreshToken"]})
    assert refreshed.status_code == 200
    assert refreshed.json()["accessToken"]


def test_lounge_creation_join_and_seeded_segue_chain(client, host_headers, listener_headers):
    lounge = make_lounge(client, host_headers, "Chain Lounge")
    assert lounge["inviteCode"].startswith("SP-")
    state = client.get(f"/api/v1/lounges/{lounge['loungeId']}/state", headers=host_headers).json()
    assert state["nowPlaying"]["trackId"] == "t_test_night"
    assert len(state["queue"]) >= 3
    # The seeded queue is built from rule-compatible tracks only.
    assert all(item["track"]["acoustic"] if "acoustic" in item["track"] else True for item in state["queue"])

    joined = client.post(
        "/api/v1/lounges/join", headers=listener_headers, json={"inviteCode": lounge["inviteCode"]}
    )
    assert joined.status_code == 200
    assert joined.json()["loungeId"] == lounge["loungeId"]
    assert joined.json()["state"]["nowPlaying"]["trackId"] == "t_test_night"


def test_queue_vote_syncs_identically_to_both_clients(client, host_headers):
    lounge = make_lounge(client, host_headers, "Sync Lounge")
    lounge_id = lounge["loungeId"]
    host_token = access_token(client, "host@test.dev")
    listener_token = access_token(client, "listener@test.dev")
    listener2_token = access_token(client, "listener2@test.dev")

    with client.websocket_connect(f"/ws/lounges/{lounge_id}?token={host_token}") as ws_a, \
         client.websocket_connect(f"/ws/lounges/{lounge_id}?token={listener_token}") as ws_b:
        state_a = _recv_until(ws_a, "lounge.state")
        _recv_until(ws_b, "lounge.state")
        entry = state_a["payload"]["queue"][-1]

        ws_b.send_json(
            {"event": "lounge.queue.vote", "payload": {"entryId": entry["entryId"]}}
        )
        update_a = _recv_until(ws_a, "lounge.queue.update")
        update_b = _recv_until(ws_b, "lounge.queue.update")
        order_a = [item["entryId"] for item in update_a["payload"]["queue"]]
        order_b = [item["entryId"] for item in update_b["payload"]["queue"]]
        assert order_a == order_b
        assert order_a[0] == entry["entryId"]

        # A brand-new participant receives the full current state on connect.
        with client.websocket_connect(
            f"/ws/lounges/{lounge_id}?token={listener2_token}"
        ) as ws_c:
            late = _recv_until(ws_c, "lounge.state")
            assert [item["entryId"] for item in late["payload"]["queue"]] == order_a
            assert late["payload"]["nowPlaying"]["trackId"] == state_a["payload"]["nowPlaying"]["trackId"]


def test_chat_sentiment_and_history(client, host_headers):
    lounge = make_lounge(client, host_headers, "Chat Lounge")
    lounge_id = lounge["loungeId"]
    token = access_token(client, "listener@test.dev")

    positive = client.post(
        f"/api/v1/lounges/{lounge_id}/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"body": "absolutely love this, best set ever 🔥"},
    )
    assert positive.status_code == 200
    assert positive.json()["sentimentBucket"] == "positive"

    negative = client.post(
        f"/api/v1/lounges/{lounge_id}/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"body": "this is boring and dead, skip it"},
    )
    assert negative.json()["sentimentBucket"] == "negative"

    history = client.get(f"/api/v1/lounges/{lounge_id}/chat", headers=host_headers).json()
    assert len(history["messages"]) >= 2
    assert {message["sentimentBucket"] for message in history["messages"]} >= {"positive", "negative"}


def test_grounded_ai_qa_and_declines_unknown_facts(client, host_headers):
    lounge = make_lounge(client, host_headers, "RAG Lounge")
    lounge_id = lounge["loungeId"]
    grounded = client.post(
        f"/api/v1/lounges/{lounge_id}/ai/ask",
        headers=host_headers,
        json={"question": "where is the vocal chop sampled from?"},
    )
    assert grounded.status_code == 200, grounded.text
    payload = grounded.json()
    assert payload["grounded"] is True
    assert payload["sourceEvents"]
    assert any(citation["type"] == "sample_credit" for citation in payload["citations"])
    assert "1981" in payload["answer"]

    ungrounded = client.post(
        f"/api/v1/lounges/{lounge_id}/ai/ask",
        headers=host_headers,
        json={"question": "which director adapted this into a 1994 film?"},
    )
    assert ungrounded.status_code == 200
    assert ungrounded.json()["grounded"] is False
    assert ungrounded.json()["sourceEvents"] == []


def test_segue_endpoint_is_explainable_and_compliant(client, host_headers):
    lounge = make_lounge(client, host_headers, "Segue Lounge")
    state = client.get(f"/api/v1/lounges/{lounge['loungeId']}/state", headers=host_headers).json()
    candidates = [item["trackId"] for item in state["queue"]]
    response = client.post(
        f"/api/v1/lounges/{lounge['loungeId']}/ai/segue",
        headers=host_headers,
        json={
            "currentTrackId": state["nowPlaying"]["trackId"],
            "currentTs": 20,
            "candidateTrackIds": candidates,
            "currentSentimentScore": 0.5,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["selectedTrackId"] in candidates
    assert payload["matchReason"]["camelotRelation"] in ("identical", "adjacent", "relative")
    assert payload["matchReason"]["bpmDeltaPct"] <= 8.0
    assert payload["sourceEvents"]
    assert payload["ranked"][0]["valid"] is True


def test_soundclash_submit_vote_resolve_and_splice(client, host_headers):
    lounge = make_lounge(client, host_headers, "Battle Lounge")
    lounge_id = lounge["loungeId"]
    listener_headers = auth_headers(client, "listener@test.dev")
    listener2_headers = auth_headers(client, "listener2@test.dev")

    first = client.post(
        f"/api/v1/lounges/{lounge_id}/soundclash/submit",
        headers=listener_headers,
        json={"trackId": "t_test_after"},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"/api/v1/lounges/{lounge_id}/soundclash/submit",
        headers=listener2_headers,
        json={"trackId": "t_test_neon"},
    )
    assert second.status_code == 200, second.text
    battle = second.json()["battle"]
    assert len(battle["candidates"]) == 2

    winner_candidate = battle["candidates"][0]["candidateId"]
    loser_candidate = battle["candidates"][1]["candidateId"]
    assert (
        client.post(
            f"/api/v1/lounges/{lounge_id}/soundclash/vote",
            headers=listener_headers,
            json={"candidateId": winner_candidate},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/lounges/{lounge_id}/soundclash/vote",
            headers=listener2_headers,
            json={"candidateId": loser_candidate},
        ).status_code
        == 200
    )

    host_token = access_token(client, "host@test.dev")
    with client.websocket_connect(f"/ws/lounges/{lounge_id}?token={host_token}") as ws:
        _recv_until(ws, "lounge.state")
        resolved = client.post(
            f"/api/v1/lounges/{lounge_id}/soundclash/resolve", headers=host_headers
        )
        assert resolved.status_code == 200, resolved.text
        event = _recv_until(ws, "lounge.soundclash.resolved")
        winning_track = resolved.json()["winningTrackId"]
        assert event["payload"]["winningTrackId"] == winning_track

        queue_event = _recv_until(ws, "lounge.queue.update")
        assert queue_event["payload"]["queue"][0]["trackId"] == winning_track


def test_analytics_reconcile_with_chat_log(client, host_headers):
    lounge = make_lounge(client, host_headers, "Analytics Lounge")
    lounge_id = lounge["loungeId"]
    token = access_token(client, "listener@test.dev")
    for body in ("this is fire", "so good", "boring now", "much better"):
        assert (
            client.post(
                f"/api/v1/lounges/{lounge_id}/chat",
                headers={"Authorization": f"Bearer {token}"},
                json={"body": body},
            ).status_code
            == 200
        )
    history = client.get(f"/api/v1/lounges/{lounge_id}/chat", headers=host_headers).json()
    analytics = client.get(f"/api/v1/lounges/{lounge_id}/analytics", headers=host_headers).json()
    assert analytics["chatMessageCount"] == history["count"]
    assert analytics["sentimentSampleCount"] == history["count"]
    assert len(analytics["sentimentTimeline"]) >= 1
    assert sum(bucket["messageCount"] for bucket in analytics["sentimentTimeline"]) == history["count"]


def test_host_disconnect_freezes_queue_until_return(client, host_headers, listener_headers):
    lounge = make_lounge(client, host_headers, "Freeze Lounge")
    lounge_id = lounge["loungeId"]
    host_token = access_token(client, "host@test.dev")
    listener_token = access_token(client, "listener@test.dev")

    with client.websocket_connect(f"/ws/lounges/{lounge_id}?token={host_token}") as host_ws:
        _recv_until(host_ws, "lounge.state")
        with client.websocket_connect(
            f"/ws/lounges/{lounge_id}?token={listener_token}"
        ) as listener_ws:
            state = _recv_until(listener_ws, "lounge.state")
            assert state["payload"]["lounge"]["frozen"] is False
            listener_ws.send_json(
                {"event": "lounge.queue.add", "payload": {"trackId": "t_test_double"}}
            )
            added = _recv_until(listener_ws, "lounge.queue.update")
            assert added
        time.sleep(0.2)

    # Host is gone: the room freezes queue mutation deterministically.
    with client.websocket_connect(f"/ws/lounges/{lounge_id}?token={listener_token}") as listener_ws:
        state = _recv_until(listener_ws, "lounge.state")
        assert state["payload"]["lounge"]["frozen"] is True
        listener_ws.send_json(
            {"event": "lounge.queue.add", "payload": {"trackId": "t_test_bad_key"}}
        )
        error = _recv_until(listener_ws, "lounge.error")
        assert "frozen" in error["payload"]["message"].lower()


def test_reconnect_resyncs_current_state(client, host_headers):
    lounge = make_lounge(client, host_headers, "Reconnect Lounge")
    lounge_id = lounge["loungeId"]
    token = access_token(client, "listener@test.dev")
    with client.websocket_connect(f"/ws/lounges/{lounge_id}?token={token}") as ws:
        first = _recv_until(ws, "lounge.state")
    with client.websocket_connect(f"/ws/lounges/{lounge_id}?token={token}") as ws:
        second = _recv_until(ws, "lounge.state")
    assert first["payload"]["nowPlaying"]["trackId"] == second["payload"]["nowPlaying"]["trackId"]
    assert len(second["payload"]["queue"]) == len(first["payload"]["queue"])
