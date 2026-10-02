"""Objects and the ObjectStore — the Unified Persistent Origin (step 1.4).

Constitution section 3 in full:

  * Every change appends a new version. Objects are defined by their entire history.
  * There is no "save" operation and no "unsaved" state.
  * Once a version has been acknowledged, it is durable.
  * Retention is tiered: recent versions at full fidelity, pins never
    auto-removed, older history may be semantically compacted.
  * True ephemeral Objects are forbidden.
  * The fast-memory / long-term-storage split is hidden from Units and humans.

Two consequences worth stating plainly, because they shape the whole API:

There is no `save()`. `append()` *is* the write, and it returns an acknowledged
version. A Unit cannot hold a modified Object in a dirty state, because it never
holds a mutable Object at all — it holds an id plus a Capability, and asks.

There is no `list()`, no `keys()`, no `exists()`. Section 5 says discovery
without a relevant Capability is impossible, and object ids are 128-bit secrets,
so the only way to reach an Object is to already hold a token naming it. The
absence of an enumeration method is not an omission; it is the invariant.

Phase 1 note: this is a pure simulation, so "durable" means acknowledged and
committed to the store for the lifetime of the process. Surviving process death
is Phase 2 (Persistent Object Substrate). The `acked` flag below is the seam
where a real write-ahead log will attach, and nothing in this module assumes the
backing store is memory.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import constitution
from .capability import Capability, Right
from .ids import new_id


@dataclass(frozen=True)
class Version:
    """One immutable moment in an Object's history.

    Versions are never edited or reordered. `seq` is dense and monotonic within
    an Object, which is what makes "defined by their entire history" a literal
    property rather than a slogan.
    """

    seq: int
    payload: Any
    author: str
    step: int
    note: str = ""
    acked: bool = True

    def describe(self, *, payload: bool = True) -> dict:
        d = {
            "seq": self.seq,
            "author": self.author,
            "step": self.step,
            "note": self.note,
            "acked": self.acked,
        }
        if payload:
            d["payload"] = self.payload
        return d


class ObjectNotFound(KeyError):
    """Raised when an id names no Object.

    Intentionally indistinguishable from "you may not know about this Object".
    Collapsing the two cases is what makes discovery-by-probing impossible: an
    attacker learns nothing from a miss.
    """


class CompactedError(RuntimeError):
    """A version's payload was reclaimed by compaction.

    Its metadata survives — invariant 7 forbids silently losing anything the
    human was told exists — but the bytes are gone. Re-derive from a pin, or
    from a later version.
    """


@dataclass
class Object:
    """An append-only, fully versioned entity.

    `pins` and `preferred` are pointers *into* history, not modifications of it.
    Pinning never rewrites a Version; it only marks one as permanently
    retainable. `preferred` is how the Improver marks a better version of a
    Unit's code without destroying the worse one — section 7 requires that all
    improvements stay reversible, and reversibility here means the old version
    is still addressable and can be re-preferred.
    """

    object_id: str
    kind: str
    created_step: int
    versions: list[Version] = field(default_factory=list)
    pins: set[int] = field(default_factory=set)
    #: Seqs whose payload has been reclaimed. Metadata is retained forever.
    compacted: set[int] = field(default_factory=set)
    preferred: Optional[int] = None

    def __post_init__(self) -> None:
        # Section 11: true ephemeral Objects are forbidden at the foundation.
        constitution.require(
            3, bool(self.kind), "Object created without a kind — ephemeral Objects are forbidden"
        )

    @property
    def latest_seq(self) -> int:
        return self.versions[-1].seq if self.versions else -1

    def latest(self) -> Version:
        if not self.versions:
            raise CompactedError(f"object {self.object_id} has no versions")
        return self.versions[-1]

    def get(self, seq: int) -> Version:
        for v in self.versions:
            if v.seq == seq:
                return v
        raise ObjectNotFound(f"object {self.object_id} has no version {seq}")

    def effective(self) -> Version:
        """The version a consumer should use: preferred if set, else latest."""
        if self.preferred is not None:
            return self.get(self.preferred)
        return self.latest()

    def describe(self, *, payloads: bool = False) -> dict:
        return {
            "object_id": self.object_id,
            "kind": self.kind,
            "created_step": self.created_step,
            "version_count": len(self.versions),
            "latest_seq": self.latest_seq,
            "preferred": self.preferred,
            "pins": sorted(self.pins),
            "compacted": sorted(self.compacted),
            "versions": [v.describe(payload=payloads) for v in self.versions] if payloads else None,
        }


#: The Nucleus-shaped hole the store checks authority through. Keeping this a
#: bare callable rather than a Nucleus import is what stops the ObjectStore from
#: depending on privileged code — it is an ordinary service that happens to be
#: given a validator at birth.
Validator = Callable[[Capability, Right, Optional[str], str], Any]


def _json_default(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    if hasattr(value, "__dict__"):
        return value.__dict__
    return str(value)


class ObjectStore:
    """Capability-gated append-only storage.

    Every method takes the acting principal's id and the Capability it claims to
    act under, and refuses without both. There is no unguarded path through this
    class, including for the Nucleus: the core holds no ambient authority over
    Objects either (invariant 2).
    """

    def __init__(self, validator: Validator, *, storage_path: Optional[str | os.PathLike[str]] = None) -> None:
        self._objects: dict[str, Object] = {}
        self._validate = validator
        self._storage_path = Path(storage_path) if storage_path is not None else None
        if self._storage_path is not None:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            if self._storage_path.exists() or self._storage_path.with_suffix(f"{self._storage_path.suffix}.tmp").exists():
                self._load()

    def _snapshot(self) -> dict[str, dict]:
        return {
            object_id: {
                "kind": obj.kind,
                "created_step": obj.created_step,
                "versions": [
                    {
                        "seq": v.seq,
                        "payload": v.payload,
                        "author": v.author,
                        "step": v.step,
                        "note": v.note,
                        "acked": v.acked,
                    }
                    for v in obj.versions
                ],
                "pins": sorted(obj.pins),
                "compacted": sorted(obj.compacted),
                "preferred": obj.preferred,
            }
            for object_id, obj in self._objects.items()
        }

    def _persist(self) -> None:
        if self._storage_path is None:
            return
        tmp_path = self._storage_path.with_suffix(f"{self._storage_path.suffix}.tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(self._snapshot(), handle, sort_keys=True, default=_json_default)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self._storage_path)
        try:
            dir_fd = os.open(str(self._storage_path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (AttributeError, OSError, NotImplementedError):
            pass

    def _load(self) -> None:
        if self._storage_path is None:
            return
        candidate = self._storage_path
        if not candidate.exists():
            tmp = candidate.with_suffix(f"{candidate.suffix}.tmp")
            if tmp.exists():
                candidate = tmp
        if not candidate.exists():
            return
        with open(candidate, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self._objects = {}
        for object_id, item in payload.items():
            obj = Object(
                object_id=object_id,
                kind=item["kind"],
                created_step=item.get("created_step", 0),
            )
            obj.versions = [
                Version(
                    seq=v["seq"],
                    payload=v["payload"],
                    author=v["author"],
                    step=v["step"],
                    note=v.get("note", ""),
                    acked=v.get("acked", True),
                )
                for v in item.get("versions", [])
            ]
            obj.pins = set(item.get("pins", []))
            obj.compacted = set(item.get("compacted", []))
            obj.preferred = item.get("preferred")
            self._objects[object_id] = obj

    # --- authority -------------------------------------------------------
    def _check(self, cap: Optional[Capability], right: Right, target: Optional[str], holder: str) -> None:
        if cap is None:
            raise PermissionError(f"{holder} attempted {right.value} on {target} with no Capability")
        self._validate(cap, right, target, holder)

    # --- write path ------------------------------------------------------
    def create(
        self,
        holder: str,
        kind: str,
        payload: Any,
        cap: Optional[Capability],
        *,
        note: str = "",
        step: int = 0,
        object_id: Optional[str] = None,
        acked: bool = True,
    ) -> Object:
        """Bring a new persistent Object into existence with its first version.

        Requires APPEND held namespace-wide (target None). An APPEND scoped to an
        existing Object authorises extending that Object only, not allocating new
        storage — the distinction is the difference between "may edit this" and
        "may create".
        """
        self._check(cap, Right.APPEND, None, holder)
        oid = object_id or new_id("obj")
        if oid in self._objects:
            raise ObjectNotFound(f"object id collision: {oid}")
        obj = Object(object_id=oid, kind=kind, created_step=step)
        obj.versions.append(
            Version(seq=0, payload=payload, author=holder, step=step, note=note or "genesis", acked=acked)
        )
        self._objects[oid] = obj
        self._persist()
        return obj

    def append(
        self,
        holder: str,
        object_id: str,
        payload: Any,
        cap: Optional[Capability],
        *,
        note: str = "",
        step: int = 0,
        acked: bool = True,
    ) -> Version:
        """The only write operation in Origin. There is no save()."""
        obj = self._object(object_id)
        # A token scoped to this Object and a namespace-wide APPEND token both
        # satisfy this check — CapabilityRecord.grants treats target None as
        # covering every target, so there is one path, not two.
        self._check(cap, Right.APPEND, object_id, holder)
        return self._do_append(obj, holder, payload, note, step, acked)

    def _do_append(self, obj: Object, holder: str, payload: Any, note: str, step: int, acked: bool) -> Version:
        v = Version(seq=obj.latest_seq + 1, payload=payload, author=holder, step=step, note=note, acked=acked)
        obj.versions.append(v)
        self._persist()
        return v

    # --- read path -------------------------------------------------------
    def _object(self, object_id: str) -> Object:
        try:
            return self._objects[object_id]
        except KeyError:
            raise ObjectNotFound(object_id) from None

    def read(
        self, holder: str, object_id: str, cap: Optional[Capability], seq: Optional[int] = None
    ) -> Version:
        obj = self._object(object_id)
        self._check(cap, Right.READ, object_id, holder)
        v = obj.get(seq) if seq is not None else obj.effective()
        if v.seq in obj.compacted:
            raise CompactedError(f"{object_id}@{v.seq} payload was compacted; metadata retained")
        return v

    def acknowledge(
        self,
        holder: str,
        object_id: str,
        seq: int,
        cap: Optional[Capability],
        *,
        acked: bool = True,
        note: str = "",
    ) -> Version:
        """Mark a stored version as acknowledged or not acknowledged.

        This is the first explicit durability seam for Phase 2. It preserves the
        version history while updating the persistence state on the selected
        version. A frozen dataclass cannot be mutated in place, so we replace the
        entry while preserving seq, author, step, payload and metadata shape.
        """
        obj = self._object(object_id)
        self._check(cap, Right.APPEND, object_id, holder)
        idx = next((i for i, v in enumerate(obj.versions) if v.seq == seq), None)
        if idx is None:
            raise ObjectNotFound(f"object {object_id} has no version {seq}")

        current = obj.versions[idx]
        updated = Version(
            seq=current.seq,
            payload=current.payload,
            author=current.author,
            step=current.step,
            note=note or current.note,
            acked=acked,
        )
        obj.versions[idx] = updated
        self._persist()
        return updated

    def durable_versions(self, holder: str, object_id: str, cap: Optional[Capability]) -> list[Version]:
        """Return all acknowledged versions for an object.

        This acts as the Phase 2 seam where a durability layer can distinguish
        between fully committed versions and versions still pending write-ahead
        or replay after a crash.
        """
        obj = self._object(object_id)
        self._check(cap, Right.READ, object_id, holder)
        return [v for v in obj.versions if v.acked]

    def history(self, holder: str, object_id: str, cap: Optional[Capability]) -> list[dict]:
        """Full metadata for every version, payloads omitted.

        History is a distinct right from READ: being able to see *that* something
        changed, and by whom, is a different grant from being able to see the
        contents. The Console needs the former for transparency far more often
        than the latter.
        """
        obj = self._object(object_id)
        self._check(cap, Right.HISTORY, object_id, holder)
        return [
            {**v.describe(payload=False), "pinned": v.seq in obj.pins, "compacted": v.seq in obj.compacted}
            for v in obj.versions
        ]

    def describe(self, holder: str, object_id: str, cap: Optional[Capability]) -> dict:
        obj = self._object(object_id)
        self._check(cap, Right.AUDIT, object_id, holder)
        return obj.describe()

    def enumerate(self, holder: str, cap: Optional[Capability]) -> list[dict]:
        """Metadata for every Object, payloads omitted.

        This looks like it contradicts section 5's "discovery of Objects without a
        relevant Capability is impossible". It does not, because AUDIT *is* a
        relevant Capability, and section 8 separately promises the human can
        always inspect Objects. The invariant being protected is discovery
        without authority — an untokened caller still gets nothing, and object
        ids remain unguessable, so a leaked id without a token is useless.

        Only namespace-wide AUDIT reaches this; a token scoped to one Object
        authorises describing that Object and nothing more.
        """
        self._check(cap, Right.AUDIT, None, holder)
        return [
            {
                "object_id": o.object_id,
                "kind": o.kind,
                "versions": len(o.versions),
                "latest_seq": o.latest_seq,
                "preferred": o.preferred,
                "pins": sorted(o.pins),
            }
            for o in self._objects.values()
        ]

    # --- retention -------------------------------------------------------
    def pin(self, holder: str, object_id: str, seq: int, cap: Optional[Capability]) -> None:
        """Bookmark a version so it is never auto-removed (section 3)."""
        obj = self._object(object_id)
        self._check(cap, Right.PIN, object_id, holder)
        obj.get(seq)  # raises if the version never existed
        obj.pins.add(seq)
        self._persist()

    def prefer(self, holder: str, object_id: str, seq: int, cap: Optional[Capability]) -> None:
        """Mark a version as the one consumers should use.

        This is the Improver's lever and it is deliberately not a write: the
        superseded version stays in history, so preferring an older seq is a
        complete rollback.
        """
        obj = self._object(object_id)
        self._check(cap, Right.APPEND, object_id, holder)
        obj.get(seq)
        obj.preferred = seq
        self._persist()

    def compact(self, holder: str, object_id: str, cap: Optional[Capability], *, keep_recent: int = 3) -> dict:
        """Reclaim payloads of old, unpinned versions.

        Tiered retention, not deletion. Pinned versions are untouched no matter
        how old they are. Compacted versions keep their seq, author, step and
        note forever, and the compaction itself is appended as a new version
        naming exactly what it reclaimed — so the history still accounts for
        every moment it ever had (invariant 7).

        The newest version must always be retained, even if a caller requests a
        zero-count retention window; otherwise the object can lose its current
        effective state while still pretending to be durable.
        """
        obj = self._object(object_id)
        self._check(cap, Right.PIN, object_id, holder)

        keep_recent = max(1, int(keep_recent))
        cutoff = obj.latest_seq - keep_recent
        reclaimed: list[int] = []
        for v in obj.versions:
            if v.seq > cutoff or v.seq in obj.pins or v.seq in obj.compacted:
                continue
            reclaimed.append(v.seq)

        obj.compacted.update(reclaimed)
        for seq in reclaimed:
            idx = next((i for i, v in enumerate(obj.versions) if v.seq == seq), None)
            if idx is None:
                continue
            current = obj.versions[idx]
            obj.versions[idx] = Version(
                seq=current.seq,
                payload=None,
                author=current.author,
                step=current.step,
                note=current.note,
                acked=current.acked,
            )

        if reclaimed:
            self._do_append(
                obj,
                holder,
                {
                    "kind": "compaction",
                    "reclaimed": reclaimed,
                    "retained_pins": sorted(obj.pins),
                    "kept_recent": keep_recent,
                },
                note=f"compacted {len(reclaimed)} version payload(s); metadata retained",
                step=obj.versions[-1].step,
                acked=True,
            )
        self._persist()
        return {"reclaimed": reclaimed, "pins_preserved": sorted(obj.pins)}
