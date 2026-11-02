"""Pure queue state machine.

Deliberately free of I/O so the ordering rules can be unit-tested and, crucially,
so a whole reorder is computed *then* applied atomically (no client can observe a
partially-applied reorder).

Ordering: descending vote_count, then insertion order (FIFO) as a stable tiebreak.
Votes are recorded per user and are idempotent/reversible. Positions are sparse
integers (step 1000) to keep a reorder cheap to persist.
"""

from __future__ import annotations

from dataclasses import dataclass, field

POSITION_STEP = 1000


@dataclass
class QueueItem:
    entry_id: str
    track_id: str
    position: int
    vote_count: int = 0
    added_by: str | None = None
    added_seq: int = 0
    voters: set[str] = field(default_factory=set)


class QueueState:
    def __init__(self) -> None:
        self._items: dict[str, QueueItem] = {}
        self._seq = 0

    # -- queries ---------------------------------------------------------------
    def items(self) -> list[QueueItem]:
        return sorted(self._items.values(), key=lambda item: item.position)

    def get(self, entry_id: str) -> QueueItem | None:
        return self._items.get(entry_id)

    def has_track(self, track_id: str) -> bool:
        return any(item.track_id == track_id for item in self._items.values())

    def track_ids(self) -> list[str]:
        return [item.track_id for item in self.items()]

    # -- mutations -------------------------------------------------------------
    def add(self, entry_id: str, track_id: str, added_by: str | None) -> QueueItem:
        self._seq += 1
        position = self._seq * POSITION_STEP
        item = QueueItem(
            entry_id=entry_id,
            track_id=track_id,
            position=position,
            added_by=added_by,
            added_seq=self._seq,
        )
        self._items[entry_id] = item
        return item

    def remove(self, entry_id: str) -> QueueItem | None:
        return self._items.pop(entry_id, None)

    def vote(self, entry_id: str, user_id: str) -> bool:
        """Toggle a user's vote. Returns True when a vote was added."""
        item = self._items.get(entry_id)
        if item is None:
            raise KeyError(entry_id)
        if user_id in item.voters:
            item.voters.discard(user_id)
            item.vote_count = max(0, item.vote_count - 1)
            return False
        item.voters.add(user_id)
        item.vote_count += 1
        return True

    def reorder(self) -> list[dict]:
        """Recompute positions atomically and return the new ordering."""
        ordered = sorted(
            self._items.values(),
            key=lambda item: (-item.vote_count, item.added_seq),
        )
        for index, item in enumerate(ordered, start=1):
            item.position = index * POSITION_STEP
        if ordered:
            self._seq = max(self._seq, len(ordered))
        return self.snapshot()

    def promote(self, entry_id: str) -> list[dict]:
        """Force an entry to the top of Next Up, preserving the rest of the order."""
        item = self._items.get(entry_id)
        if item is None:
            raise KeyError(entry_id)
        ordered = [entry for entry in self.items() if entry.entry_id != entry_id]
        ordered.insert(0, item)
        for index, entry in enumerate(ordered, start=1):
            entry.position = index * POSITION_STEP
        self._seq = max(self._seq, len(ordered))
        return self.snapshot()

    def snapshot(self) -> list[dict]:
        return [
            {
                "entryId": item.entry_id,
                "trackId": item.track_id,
                "position": item.position,
                "voteCount": item.vote_count,
                "addedBy": item.added_by,
            }
            for item in self.items()
        ]

    def load(self, rows: list[dict]) -> None:
        """Hydrate from persisted rows after a restart."""
        self._items.clear()
        self._seq = 0
        for row in rows:
            self._seq += 1
            item = QueueItem(
                entry_id=row["entryId"],
                track_id=row["trackId"],
                position=int(row["position"]),
                vote_count=int(row.get("voteCount", 0)),
                added_by=row.get("addedBy"),
                added_seq=self._seq,
                voters=set(row.get("voters", [])),
            )
            self._items[item.entry_id] = item
        self._seq = max(self._seq, len(self._items))

    def replace_first(self, track_id: str, added_by: str | None = None) -> None:
        """Splice a winning track into the front of Next Up (Soundclash resolution)."""
        items = self.items()
        if items:
            items[0].track_id = track_id
            items[0].vote_count = 0
            items[0].voters.clear()
            items[0].added_by = added_by
        else:
            self.add(f"sc_splice_{track_id}", track_id, added_by)
