"""Server-side RBAC for all four roles, plus upload validation."""

from __future__ import annotations

import time

from conftest import access_token, auth_headers, make_lounge


def test_unauthenticated_requests_are_rejected(client):
    assert client.get("/api/v1/tracks").status_code == 401
    assert client.get("/api/v1/me/dashboard").status_code == 401
    assert client.post("/api/v1/lounges", json={}).status_code == 401


def test_refresh_token_cannot_be_used_as_access_token(client):
    response = client.post(
        "/api/v1/auth/login", json={"email": "host@test.dev", "password": "Password123!"}
    )
    refresh = response.json()["refreshToken"]
    assert client.get("/api/v1/tracks", headers={"Authorization": f"Bearer {refresh}"}).status_code == 401


def test_listener_cannot_manage_lounge(client, host_headers, listener_headers):
    lounge = make_lounge(client, host_headers, "RBAC Lounge")
    lounge_id = lounge["loungeId"]

    assert (
        client.patch(
            f"/api/v1/lounges/{lounge_id}/settings",
            headers=listener_headers,
            json={"bpmTolerancePct": 12},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/lounges/{lounge_id}/playback?action=next", headers=listener_headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/lounges/{lounge_id}/soundclash/open",
            headers=listener_headers,
            json={"durationSeconds": 20},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/lounges/{lounge_id}/soundclash/resolve", headers=listener_headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/lounges/{lounge_id}/queue/promote",
            headers=listener_headers,
            json={"entryId": "whatever"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/lounges/{lounge_id}/participants/someone/role",
            headers=listener_headers,
            json={"role": "host"},
        ).status_code
        == 403
    )


def test_host_and_codj_can_manage(client, host_headers, codj_headers):
    lounge = make_lounge(client, host_headers, "Manageable Lounge")
    lounge_id = lounge["loungeId"]
    assert (
        client.patch(
            f"/api/v1/lounges/{lounge_id}/settings",
            headers=host_headers,
            json={"bpmTolerancePct": 10, "aiVerbosity": "chatty"},
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/v1/lounges/{lounge_id}/settings",
            headers=codj_headers,
            json={"bpmTolerancePct": 9},
        ).status_code
        == 200
    )


def test_listener_cannot_create_or_edit_tracks(client, listener_headers, admin_headers):
    payload = {
        "title": "Injected Track",
        "artist": "Tester",
        "genre": "techno",
        "durationSeconds": 30,
        "acoustic": {"bpm": 124, "camelotKey": "8A", "energy": 0.7, "valence": 0.5},
        "timeline": [],
    }
    assert client.post("/api/v1/admin/tracks", headers=listener_headers, json=payload).status_code == 403
    created = client.post("/api/v1/admin/tracks", headers=admin_headers, json=payload)
    assert created.status_code == 201, created.text
    track_id = created.json()["trackId"]
    assert (
        client.patch(
            f"/api/v1/admin/tracks/{track_id}", headers=listener_headers, json={"title": "Hijack"}
        ).status_code
        == 403
    )
    assert (
        client.delete(f"/api/v1/admin/tracks/{track_id}", headers=admin_headers).status_code == 200
    )


def test_admin_users_endpoint_is_admin_only(client, host_headers, admin_headers):
    assert client.get("/api/v1/admin/users", headers=host_headers).status_code == 403
    response = client.get("/api/v1/admin/users", headers=admin_headers)
    assert response.status_code == 200
    assert {user["role"] for user in response.json()["users"]} >= {"admin", "host", "listener"}


def test_track_upload_validation(client, admin_headers):
    base = {
        "title": "Validation Test",
        "artist": "Tester",
        "genre": "techno",
        "durationSeconds": 30,
        "timeline": [],
    }
    invalid_payloads = [
        {**base, "acoustic": {"bpm": 0, "camelotKey": "8A", "energy": 0.7, "valence": 0.5}},
        {**base, "acoustic": {"bpm": 124, "camelotKey": "13A", "energy": 0.7, "valence": 0.5}},
        {**base, "acoustic": {"bpm": 124, "camelotKey": "8C", "energy": 0.7, "valence": 0.5}},
        {**base, "acoustic": {"bpm": 124, "camelotKey": "8A", "energy": 1.5, "valence": 0.5}},
        {**base, "acoustic": {"bpm": 124, "camelotKey": "8A", "energy": 0.7, "valence": -0.1}},
        {**base, "acoustic": {"bpm": -1, "camelotKey": "8A", "energy": 0.7, "valence": 0.5}},
    ]
    for payload in invalid_payloads:
        response = client.post("/api/v1/admin/tracks", headers=admin_headers, json=payload)
        assert response.status_code == 422, payload


def test_websocket_role_enforcement(client, host_headers, listener_headers):
    lounge = make_lounge(client, host_headers, "WS RBAC Lounge")
    lounge_id = lounge["loungeId"]
    host_token = access_token(client, "host@test.dev")
    listener_token = access_token(client, "listener@test.dev")
    with client.websocket_connect(f"/ws/lounges/{lounge_id}?token={host_token}") as host_ws:
        host_ws.receive_json()
        with client.websocket_connect(
            f"/ws/lounges/{lounge_id}?token={listener_token}"
        ) as listener_ws:
            listener_ws.receive_json()
            listener_ws.send_json(
                {"event": "lounge.playback.control", "payload": {"action": "next"}}
            )
            error = None
            for _ in range(6):
                message = listener_ws.receive_json()
                if message["event"] == "lounge.error":
                    error = message
                    break
            assert error is not None
            assert error["payload"]["code"] == "forbidden"


def test_websocket_rejects_bad_token(client, host_headers):
    lounge = make_lounge(client, host_headers, "Bad Token Lounge")
    from starlette.websockets import WebSocketDisconnect

    try:
        with client.websocket_connect(f"/ws/lounges/{lounge['loungeId']}?token=nope") as ws:
            ws.receive_json()
    except WebSocketDisconnect as exc:
        assert exc.code == 4401
        return
    raise AssertionError("invalid token should close the socket")


def test_listener_self_registration_never_grants_admin(client):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "newbie@test.dev", "password": "Password123!", "displayName": "Newbie"},
    )
    assert response.status_code == 201
    assert response.json()["user"]["role"] == "listener"
    assert (
        client.post(
            "/api/v1/admin/tracks",
            headers={"Authorization": f"Bearer {response.json()['accessToken']}"},
            json={"title": "x"},
        ).status_code
        == 403
    )
    time.sleep(0.01)
