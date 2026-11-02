"""Queue state machine: atomic ordering, votes, promotion and Soundclash splice."""

from __future__ import annotations

import itertools

from app.services.queue import POSITION_STEP, QueueState


def _queue_with(items: list[tuple[str, str]]) -> QueueState:
    state = QueueState()
    for entry_id, track_id in items:
        state.add(entry_id, track_id, added_by="u1")
    return state


def test_new_entries_append_in_order():
    state = _queue_with([("e1", "t1"), ("e2", "t2"), ("e3", "t3")])
    assert [item.track_id for item in state.items()] == ["t1", "t2", "t3"]
    assert [item.position for item in state.items()] == [
        POSITION_STEP,
        2 * POSITION_STEP,
        3 * POSITION_STEP,
    ]


def test_votes_reorder_by_count_then_fifo():
    state = _queue_with([("e1", "t1"), ("e2", "t2"), ("e3", "t3")])
    state.vote("e3", "u2")
    state.vote("e3", "u3")
    state.vote("e2", "u4")
    order = state.reorder()
    assert [row["trackId"] for row in order] == ["t3", "t2", "t1"]
    assert [row["voteCount"] for row in order] == [2, 1, 0]


def test_vote_is_idempotent_and_reversible():
    state = _queue_with([("e1", "t1")])
    assert state.vote("e1", "u1") is True
    assert state.vote("e1", "u1") is False
    assert state.get("e1").vote_count == 0
    assert state.vote("e1", "u1") is True
    assert state.get("e1").vote_count == 1


def test_concurrent_vote_arrival_order_cannot_change_final_order():
    """Both interleavings converge on the same deterministic ordering."""
    states = []
    for ordering in itertools.permutations([("e2", "u1"), ("e3", "u1"), ("e2", "u2")]):
        state = _queue_with([("e1", "t1"), ("e2", "t2"), ("e3", "t3")])
        for entry_id, user_id in ordering:
            state.vote(entry_id, user_id)
            state.reorder()
        states.append([row["trackId"] for row in state.snapshot()])
    assert len({tuple(order) for order in states}) == 1
    assert states[0] == ["t2", "t3", "t1"]


def test_positions_stay_unique_and_sparse_after_reorders():
    state = _queue_with([("e1", "t1"), ("e2", "t2"), ("e3", "t3")])
    for user in ("u2", "u3", "u4"):
        state.vote("e3", user)
    snapshot = state.reorder()
    positions = [row["position"] for row in snapshot]
    assert len(set(positions)) == len(positions)
    assert all(position % POSITION_STEP == 0 for position in positions)


def test_promote_moves_entry_to_front_without_losing_others():
    state = _queue_with([("e1", "t1"), ("e2", "t2"), ("e3", "t3")])
    state.promote("e3")
    assert [item.track_id for item in state.items()] == ["t3", "t1", "t2"]


def test_unknown_entry_vote_raises():
    state = QueueState()
    try:
        state.vote("missing", "u1")
    except KeyError:
        return
    raise AssertionError("expected KeyError")


def test_replace_first_splices_battle_winner_at_front():
    state = _queue_with([("e1", "t1"), ("e2", "t2")])
    state.replace_first("winner")
    assert state.items()[0].track_id == "winner"
    assert len(state.items()) == 2
    assert state.get("e1").vote_count == 0


def test_load_rehydrates_positions_and_votes():
    state = QueueState()
    state.load(
        [
            {"entryId": "e2", "trackId": "t2", "position": 1000, "voteCount": 2},
            {"entryId": "e1", "trackId": "t1", "position": 2000, "voteCount": 0},
        ]
    )
    assert [item.track_id for item in state.items()] == ["t2", "t1"]
    state.add("e3", "t3", "u9")
    assert [item.track_id for item in state.items()] == ["t2", "t1", "t3"]


def test_duplicate_track_detection():
    state = _queue_with([("e1", "t1")])
    assert state.has_track("t1") is True
    assert state.has_track("t2") is False
