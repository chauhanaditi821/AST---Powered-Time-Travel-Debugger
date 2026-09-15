"""
tracer.py
---------
Week 2 deliverable: "The Tracer - Implement sys.settrace to record the
execution flow of the target script, capturing variable states and
saving them to SQLite."

Week 3 deliverable: "Delta Compression - only save deltas (what changed)."

Design
------
sys.settrace fires a callback on every 'call', 'line', 'return' and
'exception' event. For every 'line' event we:

    1. Look at frame.f_locals (a live dict Python maintains for us - this
       is the "free" instrumentation that makes sys.settrace attractive
       vs. hand-rolled bytecode patching).
    2. Diff it against a cached snapshot of that frame's locals from the
       previous line we saw for that exact frame.
    3. Write ONE `events` row for the line, and ONE `deltas` row per
       variable that is new or changed (skipping unchanged variables --
       this is the delta compression).
    4. Cache the new locals snapshot for next time.

We only trace code that belongs to the target file (co_filename match),
so we don't drown the trace in interpreter / stdlib noise.

Frames are tracked by `id(frame)` for the lifetime of the trace. CPython
recycles frame ids after they're garbage collected, but because we only
read `id()` while the frame is alive (we're inside its call/line/return
events) this is safe within a single trace session.
"""

from __future__ import annotations

import copy
import runpy
import sys
import time
from typing import Any

from .storage import TraceStorage

# Values above this size (repr length) get truncated so a single huge
# object (e.g. a big list) can't blow up the database.
MAX_VALUE_REPR_LEN = 2000


def _safe_repr(value: Any) -> tuple[str, str]:
    """Return (repr_string, type_name) for a value, never raising."""
    type_name = type(value).__name__
    try:
        r = repr(value)
    except Exception as exc:  # noqa: BLE001 - repr() itself can raise for buggy __repr__
        r = f"<unrepresentable: {exc}>"
    if len(r) > MAX_VALUE_REPR_LEN:
        r = r[:MAX_VALUE_REPR_LEN] + f"... <truncated, {len(r)} chars total>"
    return r, type_name


def _snapshot_locals(frame) -> dict[str, Any]:
    """Shallow-copy the frame's locals dict so later mutation of the
    original objects-in-place doesn't corrupt our "previous state" cache.
    We do a best-effort deep-ish copy for common mutable containers, and
    fall back to keeping the same reference (with repr computed eagerly)
    for anything exotic.
    """
    snap = {}
    for k, v in frame.f_locals.items():
        if k.startswith("__") and k.endswith("__"):
            continue
        try:
            snap[k] = copy.copy(v) if isinstance(v, (list, dict, set, bytearray)) else v
        except Exception:  # noqa: BLE001
            snap[k] = v
    return snap


class PyChronicleTracer:
    """The core execution engine. One instance = one trace session."""

    def __init__(self, target_filename: str, db_path: str):
        self.target_filename = target_filename
        self.store = TraceStorage(db_path, mode="write")
        self.store.set_meta("target_filename", target_filename)
        self.store.set_meta("started_at", str(time.time()))

        # frame_id -> previous locals snapshot (for diffing)
        self._prev_locals: dict[int, dict[str, Any]] = {}
        # frame_id -> parent frame_id, depth
        self._frame_parent: dict[int, int | None] = {}
        self._frame_depth: dict[int, int] = {}
        self._seen_vars: dict[int, set[str]] = {}  # frame_id -> var names ever seen

        self._commit_every = 200
        self._events_since_commit = 0

    # ------------------------------------------------------------------

    def _belongs_to_target(self, frame) -> bool:
        return frame.f_code.co_filename == self.target_filename

    def _diff_and_store(self, seq: int, frame_id: int, locals_now: dict[str, Any]) -> None:
        prev = self._prev_locals.get(frame_id, {})
        seen = self._seen_vars.setdefault(frame_id, set())
        for name, value in locals_now.items():
            is_new = name not in seen
            if not is_new:
                old_value = prev.get(name)
                # Cheap inequality check; falls back to repr compare for
                # objects whose __eq__ is expensive/broken.
                try:
                    changed = old_value != value
                except Exception:  # noqa: BLE001
                    changed = repr(old_value) != repr(value)
                if not changed:
                    continue
            value_repr, value_type = _safe_repr(value)
            self.store.record_delta(seq, frame_id, name, value_repr, value_type, is_new=is_new)
            seen.add(name)
        self._prev_locals[frame_id] = locals_now

    def _maybe_commit(self) -> None:
        self._events_since_commit += 1
        if self._events_since_commit >= self._commit_every:
            self.store.commit()
            self._events_since_commit = 0

    # ------------------------------------------------------------------
    # sys.settrace callback machinery
    # ------------------------------------------------------------------

    def _local_trace(self, frame, event: str, arg):
        """Called for every line/return/exception *within* a frame we're
        already tracing (registered via the global _global_trace call
        handler returning this function)."""
        if not self._belongs_to_target(frame):
            return None

        frame_id = id(frame)

        if event == "line":
            seq = self.store.record_event(
                frame_id=frame_id,
                parent_frame_id=self._frame_parent.get(frame_id),
                depth=self._frame_depth.get(frame_id, 0),
                filename=frame.f_code.co_filename,
                function_name=frame.f_code.co_name,
                line_number=frame.f_lineno,
                event_type="line",
            )
            self._diff_and_store(seq, frame_id, _snapshot_locals(frame))
            self._maybe_commit()

        elif event == "return":
            seq = self.store.record_event(
                frame_id=frame_id,
                parent_frame_id=self._frame_parent.get(frame_id),
                depth=self._frame_depth.get(frame_id, 0),
                filename=frame.f_code.co_filename,
                function_name=frame.f_code.co_name,
                line_number=frame.f_lineno,
                event_type="return",
            )
            value_repr, value_type = _safe_repr(arg)
            self.store.record_delta(seq, frame_id, "<return value>", value_repr, value_type, is_new=True)
            self._maybe_commit()
            # clean up this frame's cache -- it's dead now
            self._prev_locals.pop(frame_id, None)
            self._seen_vars.pop(frame_id, None)
            self._frame_parent.pop(frame_id, None)
            self._frame_depth.pop(frame_id, None)

        elif event == "exception":
            exc_type, exc_value, _ = arg
            seq = self.store.record_event(
                frame_id=frame_id,
                parent_frame_id=self._frame_parent.get(frame_id),
                depth=self._frame_depth.get(frame_id, 0),
                filename=frame.f_code.co_filename,
                function_name=frame.f_code.co_name,
                line_number=frame.f_lineno,
                event_type="exception",
            )
            value_repr, value_type = _safe_repr(exc_value)
            self.store.record_delta(seq, frame_id, "<exception>", value_repr, exc_type.__name__, is_new=True)
            self._maybe_commit()

        return self._local_trace

    def _global_trace(self, frame, event: str, arg):
        """Called on every 'call' event anywhere in the process. Returns
        `self._local_trace` (to keep tracing inside this frame) only for
        frames that belong to the target file; returns None otherwise so
        stdlib / third-party calls execute at full, untraced speed.
        """
        if event != "call":
            return None
        if not self._belongs_to_target(frame):
            return None

        frame_id = id(frame)
        parent = frame.f_back
        parent_id = id(parent) if parent is not None and self._belongs_to_target(parent) else None
        self._frame_parent[frame_id] = parent_id
        self._frame_depth[frame_id] = self._frame_depth.get(parent_id, -1) + 1 if parent_id else 0

        seq = self.store.record_event(
            frame_id=frame_id,
            parent_frame_id=parent_id,
            depth=self._frame_depth[frame_id],
            filename=frame.f_code.co_filename,
            function_name=frame.f_code.co_name,
            line_number=frame.f_lineno,
            event_type="call",
        )
        # capture arguments as the initial state of the frame
        self._diff_and_store(seq, frame_id, _snapshot_locals(frame))
        self._maybe_commit()
        return self._local_trace

    # ------------------------------------------------------------------

    def run(self, argv: list[str] | None = None) -> int:
        """Execute the target script under the tracer. Returns the
        process-style exit code (0 = success)."""
        sys.settrace(self._global_trace)
        old_argv = sys.argv
        exit_code = 0
        try:
            sys.argv = [self.target_filename, *(argv or [])]
            runpy.run_path(self.target_filename, run_name="__main__")
        except SystemExit as e:
            exit_code = int(e.code) if isinstance(e.code, int) else (0 if e.code is None else 1)
        except Exception:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            exit_code = 1
        finally:
            sys.settrace(None)
            sys.argv = old_argv
            self.store.set_meta("finished_at", str(time.time()))
            self.store.set_meta("total_events", str(self.store.event_count()))
            self.store.close()
        return exit_code


def trace_script(target_filename: str, db_path: str, argv: list[str] | None = None) -> int:
    """Convenience one-shot API: trace `target_filename`, write history to
    `db_path`, return the script's exit code."""
    tracer = PyChronicleTracer(target_filename, db_path)
    return tracer.run(argv)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m pychronicle.tracer <script.py> [db_path]")
        raise SystemExit(1)
    script = sys.argv[1]
    db = sys.argv[2] if len(sys.argv) > 2 else "pychronicle_trace.db"
    code = trace_script(script, db)
    print(f"Trace complete -> {db} (exit code {code})")
