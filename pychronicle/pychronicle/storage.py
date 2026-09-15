"""
storage.py
----------
Week 1 deliverable: "Storage Schema - Design a fast SQLite schema to store
(timestamp, line_number, variable_name, serialized_value)."

Week 3 deliverable: "Delta Compression - Optimize the tracer to only save
deltas (what changed) rather than the entire state tree at every line."

This module owns the SQLite schema and all read/write access. It is used
by both the tracer (writer, during execution) and the TUI (reader, during
replay). Everything is stored as *deltas*: one row per (line executed,
variable that changed), never a full snapshot of every variable.

To reconstruct "the full state at event N" the TUI folds all delta rows
for a given call-frame, in seq order, up to N. See `get_state_at`.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- One row per execution "step" (a line being entered / a call / a return).
-- This is the timeline the user scrubs through.
CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      REAL    NOT NULL,
    frame_id       INTEGER NOT NULL,
    parent_frame_id INTEGER,
    depth          INTEGER NOT NULL,
    filename       TEXT    NOT NULL,
    function_name  TEXT    NOT NULL,
    line_number    INTEGER NOT NULL,
    event_type     TEXT    NOT NULL   -- 'call' | 'line' | 'return' | 'exception'
);

-- One row per variable that CHANGED on a given event (the "delta").
-- Only mutated variables are written here -- this is the 90% memory
-- saving described in the Week 3 plan, vs. dumping every local every line.
CREATE TABLE IF NOT EXISTS deltas (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    seq            INTEGER NOT NULL REFERENCES events(seq),
    frame_id       INTEGER NOT NULL,
    variable_name  TEXT    NOT NULL,
    value_repr     TEXT    NOT NULL,
    value_type     TEXT    NOT NULL,
    is_new         INTEGER NOT NULL DEFAULT 0  -- 1 if variable didn't exist before this line
);

CREATE INDEX IF NOT EXISTS idx_deltas_seq ON deltas(seq);
CREATE INDEX IF NOT EXISTS idx_deltas_frame_var ON deltas(frame_id, variable_name, seq);
CREATE INDEX IF NOT EXISTS idx_events_line ON events(line_number);

-- Metadata about the run (single row).
CREATE TABLE IF NOT EXISTS run_info (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class EventRow:
    seq: int
    timestamp: float
    frame_id: int
    parent_frame_id: int | None
    depth: int
    filename: str
    function_name: str
    line_number: int
    event_type: str


class TraceStorage:
    """Thin wrapper around the SQLite trace database.

    Usage (writer, during tracing):
        store = TraceStorage(path, mode="write")
        seq = store.record_event(frame_id, parent_id, depth, filename, func, line, "line")
        store.record_delta(seq, frame_id, "x", 42, is_new=True)
        store.close()

    Usage (reader, during TUI replay):
        store = TraceStorage(path, mode="read")
        total = store.event_count()
        ev = store.get_event(seq)
        state = store.get_state_at(seq)   # folded variable state dict
    """

    def __init__(self, path: str | Path, mode: str = "write"):
        self.path = str(path)
        self.mode = mode
        if mode == "write":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path)
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        else:
            uri = f"file:{self.path}?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True)
        self.conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------
    # Writer API (used by tracer.py)
    # ------------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO run_info(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def record_event(
        self,
        frame_id: int,
        parent_frame_id: int | None,
        depth: int,
        filename: str,
        function_name: str,
        line_number: int,
        event_type: str,
        timestamp: float | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO events (timestamp, frame_id, parent_frame_id, depth, "
            "filename, function_name, line_number, event_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                timestamp if timestamp is not None else time.time(),
                frame_id,
                parent_frame_id,
                depth,
                filename,
                function_name,
                line_number,
                event_type,
            ),
        )
        return cur.lastrowid

    def record_delta(
        self,
        seq: int,
        frame_id: int,
        variable_name: str,
        value_repr: str,
        value_type: str,
        is_new: bool = False,
    ) -> None:
        self.conn.execute(
            "INSERT INTO deltas (seq, frame_id, variable_name, value_repr, value_type, is_new) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (seq, frame_id, variable_name, value_repr, value_type, int(is_new)),
        )

    def commit(self) -> None:
        self.conn.commit()

    # ------------------------------------------------------------------
    # Reader API (used by tui.py)
    # ------------------------------------------------------------------

    def event_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()
        return row["c"]

    def get_event(self, seq: int) -> EventRow | None:
        row = self.conn.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()
        if row is None:
            return None
        return EventRow(**{k: row[k] for k in row.keys()})

    def get_events_range(self, start_seq: int, end_seq: int) -> list[EventRow]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE seq BETWEEN ? AND ? ORDER BY seq", (start_seq, end_seq)
        ).fetchall()
        return [EventRow(**{k: r[k] for k in r.keys()}) for r in rows]

    def get_deltas_for_seq(self, seq: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM deltas WHERE seq = ? ORDER BY id", (seq,)
        ).fetchall()

    def get_state_at(self, seq: int) -> dict[str, dict]:
        """Fold every delta for every frame that is an ancestor-or-self of
        the frame active at `seq`, from the beginning of the trace up to
        and including `seq`, into a single {var_name: {value, type, seq}}
        snapshot. This is the "time travel" reconstruction step.
        """
        event = self.get_event(seq)
        if event is None:
            return {}

        # Walk the frame_id chain (this frame + its ancestors) so we show
        # locals visible in the current call stack, like a real debugger.
        frame_ids = [event.frame_id]
        parent = event.parent_frame_id
        seen = {event.frame_id}
        while parent is not None and parent not in seen:
            frame_ids.append(parent)
            seen.add(parent)
            prow = self.conn.execute(
                "SELECT parent_frame_id FROM events WHERE frame_id = ? LIMIT 1", (parent,)
            ).fetchone()
            parent = prow["parent_frame_id"] if prow else None

        placeholders = ",".join("?" for _ in frame_ids)
        rows = self.conn.execute(
            f"SELECT variable_name, value_repr, value_type, seq, frame_id "
            f"FROM deltas WHERE frame_id IN ({placeholders}) AND seq <= ? "
            f"ORDER BY seq ASC",
            (*frame_ids, seq),
        ).fetchall()

        state: dict[str, dict] = {}
        for r in rows:
            state[r["variable_name"]] = {
                "value": r["value_repr"],
                "type": r["value_type"],
                "last_changed_seq": r["seq"],
            }
        return state

    def get_variable_history(self, variable_name: str, frame_id: int | None = None) -> list[sqlite3.Row]:
        if frame_id is not None:
            return self.conn.execute(
                "SELECT * FROM deltas WHERE variable_name = ? AND frame_id = ? ORDER BY seq",
                (variable_name, frame_id),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM deltas WHERE variable_name = ? ORDER BY seq", (variable_name,)
        ).fetchall()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> "TraceStorage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
