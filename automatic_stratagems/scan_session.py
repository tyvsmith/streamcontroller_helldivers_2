"""Scan assignments, independent of GTK and capture transport."""

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import Lock
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class ScanSnapshot:
    assignments: Mapping[int, str | None]
    unknown_slots: frozenset[int] = frozenset()
    unconfirmed_slots: frozenset[int] = frozenset()
    replacing: bool = False
    status: str = 'idle'
    message: str = ''
    revision: int = 0
    recognized: int = 0
    unknown: int = 0
    overflow: int = 0
    last_scan_at: str | None = None


class ScanSession:
    def __init__(self):
        self._lock = Lock()
        self._serial = 0
        self._active = None
        self._unconfirmed_since = {}
        self._filters = {}
        self._last_report = None
        self._snapshot = ScanSnapshot(MappingProxyType({}))

    def snapshot(self) -> ScanSnapshot:
        with self._lock:
            return self._snapshot

    def is_active(self, token):
        with self._lock:
            return token is not None and token == self._active

    def _publish(self, **changes):
        self._snapshot = replace(
            self._snapshot, revision=self._snapshot.revision + 1, **changes)

    def latest_report(self):
        with self._lock:
            return deepcopy(self._last_report)

    def checkpoint(self):
        with self._lock:
            s = self._snapshot
            return dict(scan_number=self._serial, status=s.status, message=s.message,
                        last_report=deepcopy(self._last_report),
                        last_scan_at=s.last_scan_at, recognized=s.recognized,
                        unknown=s.unknown, overflow=s.overflow,
                        slots={str(slot): dict(id=key, filter=self._filters.get(slot, 'any'),
                               unknown=slot in s.unknown_slots,
                               unconfirmed=slot in s.unconfirmed_slots,
                               unconfirmed_since_scan=self._unconfirmed_since.get(slot))
                               for slot, key in s.assignments.items()})

    def restore(self, data, catalog):
        """Restore a validated checkpoint; saved sequences never authorize execution."""
        with self._lock:
            assignments, unknown, unconfirmed, ages, filters = {}, set(), set(), {}, {}
            seen = set()
            for number, row in data['slots'].items():
                slot, key = int(number), row['id']
                if key is not None and (key not in catalog or key in seen):
                    key = None
                    unknown.add(slot)
                assignments[slot] = key
                filters[slot] = row['filter']
                if key is not None:
                    seen.add(key)
                    if row['unconfirmed']:
                        unconfirmed.add(slot)
                        ages[slot] = row['unconfirmed_since_scan']
                elif row['unknown']:
                    unknown.add(slot)
            status = data['status']
            message = data['message']
            if status in ('scanning', 'idle'):
                status = 'partial' if any(assignments.values()) else 'idle'
                message = 'Restored assignments; previous scan interrupted' if status == 'partial' else 'No scan yet'
            if unknown and status == 'ready':
                status = 'partial'
            self._last_report = deepcopy(data.get('last_report'))
            self._serial = data['scan_number']
            self._active = None
            self._filters, self._unconfirmed_since = filters, ages
            self._publish(assignments=MappingProxyType(assignments),
                          unknown_slots=frozenset(unknown), unconfirmed_slots=frozenset(unconfirmed),
                          replacing=False, status=status, message=message,
                          recognized=data['recognized'], unknown=data['unknown'], overflow=data['overflow'],
                          last_scan_at=data['last_scan_at'])

    def begin(self, slots, *, replace=False) -> int | None:
        with self._lock:
            if self._active is not None:
                return None
            if any(type(slot) is not int or slot < 1 for slot in slots):
                raise ValueError('Slot numbers must be positive integers')
            self._filters = dict(slots) if isinstance(slots, Mapping) else dict.fromkeys(slots, 'any')
            if any(color not in ('any', 'red', 'blue', 'green', 'yellow') for color in self._filters.values()):
                raise ValueError('Invalid slot color')
            assignments = {slot: self._snapshot.assignments.get(slot)
                           for slot in sorted(set(slots))}
            self._unconfirmed_since = {slot: age for slot, age in self._unconfirmed_since.items()
                                       if slot in assignments}
            self._serial += 1
            self._active = self._serial
            self._publish(
                assignments=MappingProxyType(assignments),
                unknown_slots=self._snapshot.unknown_slots.intersection(assignments),
                unconfirmed_slots=self._snapshot.unconfirmed_slots.intersection(assignments),
                replacing=replace, status='scanning',
                message='Replacing stratagems' if replace else 'Scanning stratagems')
            return self._active

    def reconcile(self, slots, *, affected=None) -> bool:
        """Keep only assignments whose current slot and filter still match."""
        with self._lock:
            filters = dict(slots) if isinstance(slots, Mapping) else dict.fromkeys(slots, 'any')
            if any(type(slot) is not int or slot < 1 for slot in filters):
                raise ValueError('Slot numbers must be positive integers')
            if any(color not in ('any', 'red', 'blue', 'green', 'yellow')
                   for color in filters.values()):
                raise ValueError('Invalid slot color')
            affected = set(filters).union(self._filters) if affected is None else set(affected)
            assignments = dict(self._snapshot.assignments)
            next_filters = dict(self._filters)
            unknown = set(self._snapshot.unknown_slots)
            unconfirmed = set(self._snapshot.unconfirmed_slots)
            for slot in affected:
                if slot not in filters:
                    assignments.pop(slot, None)
                    next_filters.pop(slot, None)
                    unknown.discard(slot)
                    unconfirmed.discard(slot)
                    continue
                color = filters[slot]
                if self._filters.get(slot) != color:
                    assignments[slot] = None
                    unknown.discard(slot)
                    unconfirmed.discard(slot)
                else:
                    assignments.setdefault(slot, None)
                next_filters[slot] = color
            ages = {slot: age for slot, age in self._unconfirmed_since.items()
                    if slot in unconfirmed}
            if (next_filters == self._filters
                    and assignments == dict(self._snapshot.assignments)
                    and frozenset(unknown) == self._snapshot.unknown_slots
                    and frozenset(unconfirmed) == self._snapshot.unconfirmed_slots):
                return False
            self._active = None
            self._filters = next_filters
            self._unconfirmed_since = ages
            self._publish(
                assignments=MappingProxyType(assignments),
                unknown_slots=frozenset(unknown),
                unconfirmed_slots=frozenset(unconfirmed),
                replacing=False,
                status='partial' if any(assignments.values()) or unknown else 'idle',
                message='Configuration changed; scan required',
                recognized=sum(key is not None for key in assignments.values()),
                unknown=len(unknown), overflow=0)
            return True

    def finish(self, token: int, report: Mapping, catalog: Mapping, colors: Mapping = None) -> bool:
        with self._lock:
            if self._active is None or token != self._active:
                return False
            status = report.get('status')
            rows = report.get('rows')
            if status not in ('matched', 'partial') or not isinstance(rows, list) or not rows:
                error = report.get('error') or 'No stratagems detected'
                self._finish_failure(str(error))
                return True

            # Unknown rows occupy space, but provide no identity to preserve.
            detected = []
            recognized = set()
            unknown = 0
            for row in rows:
                key = row.get('id') if isinstance(row, Mapping) else None
                if not isinstance(key, str) or key not in catalog:
                    detected.append(None)
                    unknown += 1
                elif key not in recognized:
                    detected.append(key)
                    recognized.add(key)

            self._last_report = dict(status=status, rows=[{'id': key} for key in detected])
            colors = colors or {}
            def accepts(slot, key):
                return self._filters[slot] == 'any' or self._filters[slot] == colors.get(key)

            assignments = {slot: (key if not self._snapshot.replacing
                                   and key in catalog and accepts(slot, key) else None)
                           for slot, key in self._snapshot.assignments.items()}
            surviving = {key for key in assignments.values() if key is not None}
            unconfirmed = {slot for slot, key in assignments.items()
                           if key is not None and key not in recognized}
            unconfirmed_since = {slot: self._unconfirmed_since.get(slot, token)
                                 for slot in unconfirmed}
            pending = [key for key in detected if key is not None and key not in surviving]
            # Reserve constrained slots before unrestricted ones; keep numeric order.
            vacancies = sorted((slot for slot, key in assignments.items() if key is None),
                               key=lambda slot: (self._filters[slot] == 'any', slot))
            for slot in vacancies:
                key = next((key for key in pending if accepts(slot, key)), None)
                if key is not None:
                    assignments[slot] = key
                    pending.remove(key)
            # Give every new item a same-color opportunity before cross-color eviction.
            for same_color in (True, False):
                for key in pending[:]:
                    candidates = [slot for slot in unconfirmed if accepts(slot, key)]
                    if same_color:
                        candidates = [slot for slot in candidates if colors.get(key) is not None
                                      and colors.get(assignments[slot]) == colors[key]]
                    else:
                        candidates = [slot for slot in candidates if self._filters[slot] == 'any']
                    if not candidates:
                        continue
                    slot = min(candidates, key=lambda slot: (unconfirmed_since[slot], slot))
                    assignments[slot] = key
                    pending.remove(key)
                    unconfirmed.remove(slot)
                    del unconfirmed_since[slot]
            # Unknown screen rows have no reliable color or previous identity.
            available = [slot for slot, key in assignments.items()
                         if key is None and self._filters[slot] == 'any']
            rebuilding = self._snapshot.replacing or not any(self._snapshot.assignments.values())
            unknown_slots = set(available[:unknown]) if rebuilding else set()
            overflow = len(pending)

            self._active = None
            self._unconfirmed_since = unconfirmed_since
            self._publish(
                assignments=MappingProxyType(assignments),
                unknown_slots=frozenset(unknown_slots),
                unconfirmed_slots=frozenset(unconfirmed), replacing=False,
                status='partial' if unknown or overflow or unconfirmed or status == 'partial' else 'ready',
                message=f'{len(recognized)} recognized, {len(unconfirmed)} unconfirmed, {unknown} unknown, {overflow} overflow',
                recognized=len(recognized), unknown=unknown, overflow=overflow,
                last_scan_at=datetime.now(timezone.utc).isoformat())
            return True

    def _finish_failure(self, error: str):
        self._active = None
        self._publish(status='failed', replacing=False, message=error,
                      last_scan_at=datetime.now(timezone.utc).isoformat())

    def fail(self, token: int, error: str) -> bool:
        with self._lock:
            if self._active is None or token != self._active:
                return False
            self._finish_failure(str(error))
            return True

    def cancel(self):
        with self._lock:
            self._active = None
            self._publish(status='idle', replacing=False, message='Scan cancelled')

    def clear(self):
        with self._lock:
            self._active = None
            self._last_report = None
            self._unconfirmed_since = {}
            self._publish(
                assignments=MappingProxyType(dict.fromkeys(self._snapshot.assignments)),
                unknown_slots=frozenset(), unconfirmed_slots=frozenset(),
                replacing=False, status='idle', message='Selections cleared',
                recognized=0, unknown=0, overflow=0, last_scan_at=None)
