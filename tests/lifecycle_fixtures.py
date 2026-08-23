"""Scripted fake driver and helpers for lifecycle tests.

No provider process and no model: the fake driver just emits provider-neutral
envelopes and applies them through the real durable store. It can bump the
connection epoch (reconnect), coalesce activity, and read back the reduced
provider block. Imported directly by the test modules (not a pytest plugin).
"""

from __future__ import annotations

import json
from pathlib import Path

from spindle import lifecycle as lc


def make_spool_record(store_root, spool_id: str, *, extra: dict | None = None) -> dict:
    """Write the minimal valid ``<id>.json`` a real launch would have created."""
    root = Path(store_root)
    root.mkdir(parents=True, exist_ok=True)
    record = {"id": spool_id, "status": "running", "created_at": "2026-08-23T00:00:00+00:00"}
    if extra:
        record.update(extra)
    (root / f"{spool_id}.json").write_text(json.dumps(record, indent=2))
    return record


def read_record(store_root, spool_id: str) -> dict:
    return json.loads((Path(store_root) / f"{spool_id}.json").read_text())


def read_provider(store_root, spool_id: str) -> dict | None:
    return (read_record(store_root, spool_id).get("lifecycle") or {}).get("provider")


def ev(spool_id: str, kind: str, **kw) -> lc.LifecycleEvent:
    return lc.LifecycleEvent(spool_id=spool_id, kind=kind, **kw)


class FakeDriver:
    """Applies scripted lifecycle events to a real store via ``apply_event``."""

    def __init__(self, store_root, spool_id: str, *, threshold: int = 50) -> None:
        self.store_root = Path(store_root)
        self.spool_id = spool_id
        self.epoch = 0
        self.threshold = threshold
        self.coalescer = lc.ActivityCoalescer(spool_id, threshold=threshold, epoch=0)
        self.applied = 0

    def reconnect(self) -> None:
        """Simulate transport loss + reconnect: bump the connection epoch."""
        self.flush_activity()
        self.epoch += 1
        self.coalescer = lc.ActivityCoalescer(self.spool_id, threshold=self.threshold, epoch=self.epoch)

    def emit(self, kind: str, **kw) -> dict:
        """Apply one authoritative event immediately (flushing pending activity)."""
        self.flush_activity()
        kw.setdefault("epoch", self.epoch)
        event = lc.LifecycleEvent(spool_id=self.spool_id, kind=kind, **kw)
        result = lc.apply_event(self.store_root, self.spool_id, event)
        self.applied += 1
        return result

    def emit_raw(self, event: lc.LifecycleEvent) -> dict:
        """Apply a pre-built event verbatim (for reordered/late-epoch scripting)."""
        result = lc.apply_event(self.store_root, self.spool_id, event)
        self.applied += 1
        return result

    def activity(self, summary: str | None = None, observed_at: str | None = None) -> bool:
        event = self.coalescer.observe(summary, observed_at=observed_at)
        if event is not None:
            lc.apply_event(self.store_root, self.spool_id, event)
            self.applied += 1
            return True
        return False

    def flush_activity(self, observed_at: str | None = None) -> bool:
        event = self.coalescer.flush(observed_at=observed_at)
        if event is not None:
            lc.apply_event(self.store_root, self.spool_id, event)
            self.applied += 1
            return True
        return False

    def provider(self) -> dict | None:
        return read_provider(self.store_root, self.spool_id)
