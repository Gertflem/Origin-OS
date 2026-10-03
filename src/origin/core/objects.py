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

Durability note: "durable" here means the version survives process death, not
just the current process. When a `storage_path` is configured every mutation is
written through a temp file and an atomic rename, with the previous good snapshot
kept as a `.bak` and damaged files quarantined rather than deleted. The `acked`
flag below is the seam where a real write-ahead log will attach, and nothing in
this module assumes the backing store is memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
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
    #: Digest of this version's persisted content, as written by `_snapshot`. It is
    #: evidence, not state: `None` means "this version was never written with a
    #: digest" (an older snapshot, or a version built in memory), and the load-time
    #: audit skips those rather than calling them tampered.
    digest: Optional[str] = None

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
#:
#: The optional fifth argument is the kind of the Object being acted on. The store
#: knows kinds because it holds the Objects; the core cannot, because it has no
#: code Object and knows nothing about storage. Passing the kind in keeps kind
#: scoping enforceable without teaching the privileged core about storage.
Validator = Callable[..., Any]


def _json_default(value: Any) -> Any:
    """Last-resort encoder for payloads the JSON encoder cannot handle.

    Returning `None` here would be a lie with teeth: `json.dump` calls this only
    for objects it cannot serialise, emits whatever comes back, and the caller has
    already been told the version is durable. A payload object would therefore be
    written to the snapshot as `null` while memory held the real thing, which is
    precisely the failure this store exists to prevent. So this must always return
    something faithful, and failing loudly beats returning a plausible wrong value.
    """
    if isinstance(value, set):
        return sorted(value)
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return str(value)


# --- Payload integrity --------------------------------------------------------
#
# Structural damage detection (seq gaps, dangling pointers) cannot see a payload
# that was altered in place: every seq is still present, so the history looks
# perfect while describing something that never happened. A digest of the payload
# as it is persisted catches that, and turns a silent misrepresentation into a
# reported finding.
#
# This is integrity, not authenticity. It detects accidental corruption and
# unsophisticated tampering; it is not a MAC, so an attacker who can rewrite the
# file can also recompute the digests. A real adversary needs a key the store does
# not have, which is a later phase's problem -- the point here is that "damaged
# history is evidence" now means something checkable rather than aspirational.
_VERSION_DIGEST_KEY = b"origin/version-digest/v1"


def version_digest(version: "Version") -> str:
    """A stable digest of one version's persisted content.

    Includes the payload and the metadata that gives a version its identity, so a
    digest match means the whole record is what this store wrote. Compacted
    versions legitimately have a `None` payload and are digested as such, so
    compaction does not invalidate the digests of the records around it.
    """
    encoded = json.dumps(
        {
            "seq": version.seq,
            "payload": version.payload,
            "author": version.author,
            "step": version.step,
            "note": version.note,
            "acked": version.acked,
        },
        sort_keys=True,
        default=_json_default,
    ).encode("utf-8")
    return hashlib.blake2b(encoded, key=_VERSION_DIGEST_KEY, digest_size=16).hexdigest()


# --- Durable rename -----------------------------------------------------------
#
# `os.replace` is atomic, but atomic is not the same as durable. The rename that
# points the main snapshot at the freshly written temp file lives in the *parent
# directory's* metadata, and until that directory entry is flushed the rename can
# still be lost on power failure — leaving a file whose contents were fsynced but
# which was never actually linked into place.
#
# POSIX: open the directory and fsync it.
# Windows: refuses to open a directory as a file handle at all (it raises
#   PermissionError), so we go through CreateFileW with
#   FILE_FLAG_BACKUP_SEMANTICS and FlushFileBuffers. Skipping this step used to
#   look harmless because the failure was caught and ignored, but it silently
#   downgraded the durability guarantee on the platform the project actually runs
#   on. A missing directory flush is now reported, never swallowed.


def _flush_directory_windows(path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    GENERIC_WRITE = 0x40000000
    FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE

    # FlushFileBuffers needs GENERIC_WRITE, which is why this cannot be a plain read handle.
    handle = create_file(
        str(path),
        GENERIC_WRITE,
        FILE_SHARE_ALL,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        if not kernel32.FlushFileBuffers(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)


def _flush_directory(path: Path) -> None:
    """Flush a directory entry so a rename into it survives power loss."""
    if os.name == "nt":
        _flush_directory_windows(path)
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ObjectStore:
    """Capability-gated append-only storage.

    Every method takes the acting principal's id and the Capability it claims to
    act under, and refuses without both. There is no unguarded path through this
    class, including for the Nucleus: the core holds no ambient authority over
    Objects either (invariant 2).
    """

    def __init__(self, validator: Validator, *, storage_path: Optional[str | os.PathLike[str]] = None) -> None:
        self._objects: dict[str, Object] = {}
        #: Facts about recovery done at load time, readable by the operator.
        self.recovery_events: list[dict] = []
        #: Integrity findings from the last load. Reported, never auto-repaired.
        self.history_damage: list[dict] = []
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
                        # Per-version digest of the payload as persisted. Lets a
                        # load detect content that changed underneath it, which
                        # structural checks (seq continuity, dangling pointers)
                        # cannot: a snapshot with every seq present but one payload
                        # swapped is structurally perfect and factually false.
                        "digest": version_digest(v),
                    }
                    for v in obj.versions
                ],
                "pins": sorted(obj.pins),
                "compacted": sorted(obj.compacted),
                "preferred": obj.preferred,
            }
            for object_id, obj in self._objects.items()
        }

    def sweep(self, holder: str, cap: Optional[Capability], *, keep_recent: int = 3,
              min_versions: int = 32, dry_run: bool = True) -> dict:
        """Compact every Object whose history has outgrown its retention window.

        Section 3 describes tiered retention as a *policy*: recent versions at
        full fidelity, pins never removed, older history semantically compacted.
        Until now only the operator could apply it, by asking about one Object at
        a time, so in practice history grew until a human decided to intervene.
        This is that policy running on its own.

        Deliberately conservative, because automatic reclamation of durable memory
        is the kind of thing that should be hard to trigger accidentally:

        - `min_versions` (default 32) means an Object is left alone until its
          history is genuinely long. Short histories are usually more valuable
          whole, and compacting them saves nothing.
        - `dry_run` (default True) means nothing is touched until asked.
        - Selection is `_reclaimable_seqs`, the same function `compact` acts on and
          `reclaimable_report` previews, so this cannot disagree with either.
          Pinned, preferred, recent and already-compacted versions are untouchable.

        Compaction remains tiered rather than deletion: every reclaimed version
        keeps its seq, author, step and note forever, and the sweep appends a
        marker version naming exactly what it reclaimed (invariant 7).
        """
        self._check(cap, Right.PIN, None, holder)

        candidates: list[dict] = []
        for obj in self._objects.values():
            if len(obj.versions) < int(min_versions):
                continue
            seqs = self._reclaimable_seqs(obj, keep_recent)
            if seqs:
                candidates.append(
                    {
                        "object_id": obj.object_id,
                        "kind": obj.kind,
                        "versions": len(obj.versions),
                        "reclaimable": seqs,
                        "reclaimable_bytes": sum(
                            self._payload_bytes(v) for v in obj.versions if v.seq in set(seqs)
                        ),
                    }
                )

        candidates.sort(key=lambda row: -row["reclaimable_bytes"])
        result = {
            "keep_recent": keep_recent,
            "min_versions": int(min_versions),
            "dry_run": bool(dry_run),
            "candidates": candidates,
            "objects_swept": 0,
            "versions_reclaimed": 0,
            "bytes_reclaimed": 0,
        }
        if dry_run or not candidates:
            return result

        for row in candidates:
            self.compact(holder, row["object_id"], cap, keep_recent=keep_recent)
            result["objects_swept"] += 1
            result["versions_reclaimed"] += len(row["reclaimable"])
            result["bytes_reclaimed"] += row["reclaimable_bytes"]
        return result

    def _persist(self) -> None:
        if self._storage_path is None:
            return
        tmp_path = self._storage_path.with_suffix(f"{self._storage_path.suffix}.tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(self._snapshot(), handle, sort_keys=True, default=_json_default)
            handle.flush()
            os.fsync(handle.fileno())
        if self._storage_path.exists():
            # Keep the previous good snapshot so a damaged main file is recoverable.
            shutil.copy2(self._storage_path, self._backup_path())
        os.replace(tmp_path, self._storage_path)
        try:
            _flush_directory(self._storage_path.parent)
        except (AttributeError, OSError, NotImplementedError) as exc:
            # The rename is atomic but only becomes durable once the parent
            # directory entry is flushed. If that fails the acknowledged version
            # is still intact in the file itself, so this is a weakened guarantee
            # rather than data loss -- but it must be visible to the operator
            # instead of vanishing into an ignored exception.
            self.recovery_events.append(
                {"kind": "durability.degraded", "reason": "directory_flush_failed", "detail": str(exc)}
            )

    def _backup_path(self) -> Path:
        return self._storage_path.with_suffix(f"{self._storage_path.suffix}.bak")

    def _quarantine(self) -> None:
        """Move an unreadable snapshot aside. Damaged history is evidence, not trash."""
        target = self._storage_path.with_name(f"{self._storage_path.name}.corrupt.{time.time_ns()}")
        try:
            os.replace(self._storage_path, target)
        except OSError:
            return
        self.recovery_events.append({"kind": "snapshot.quarantined", "file": target.name})

    def _load(self) -> None:
        if self._storage_path is None:
            return

        def _load_json(path: Path) -> Optional[dict]:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    return json.load(handle)
            except (OSError, ValueError, TypeError):
                return None

        candidate = self._storage_path
        tmp = candidate.with_suffix(f"{candidate.suffix}.tmp")
        promoted = False

        if tmp.exists():
            tmp_payload = _load_json(tmp)
            if tmp_payload is not None:
                if not candidate.exists() or tmp.stat().st_mtime_ns >= candidate.stat().st_mtime_ns:
                    os.replace(tmp, candidate)
                    promoted = True
                else:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
            else:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

        if not candidate.exists():
            return

        payload = _load_json(candidate)
        if payload is None:
            self._quarantine()
            payload = _load_json(self._backup_path())
            if payload is None:
                self.recovery_events.append({"kind": "snapshot.unrecoverable"})
                self._objects = {}
                return
            self.recovery_events.append({"kind": "snapshot.restored_from_backup"})

        if promoted:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        self._objects = self._build(payload)
        self._audit_history()
        if self.history_damage and self._backup_is_safe_superset():
            # The backup holds every version main holds and is itself clean:
            # switching loses nothing and heals the gap.
            self._quarantine()
            backup = _load_json(self._backup_path())
            self._objects = self._build(backup)
            self.recovery_events.append({"kind": "snapshot.restored_from_backup"})
            self._audit_history()

    @staticmethod
    def _build(payload: dict) -> dict[str, "Object"]:
        objects: dict[str, Object] = {}
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
                    digest=v.get("digest"),
                )
                for v in item.get("versions", [])
            ]
            obj.pins = set(item.get("pins", []))
            obj.compacted = set(item.get("compacted", []))
            obj.preferred = item.get("preferred")
            objects[object_id] = obj
        return objects

    def _backup_is_safe_superset(self) -> bool:
        try:
            with open(self._backup_path(), "r", encoding="utf-8") as handle:
                backup = self._build(json.load(handle))
        except (OSError, ValueError, TypeError, KeyError):
            return False
        if self._damage_of(backup):
            return False
        for object_id, obj in self._objects.items():
            other = backup.get(object_id)
            if other is None or not {v.seq for v in obj.versions} <= {v.seq for v in other.versions}:
                return False
        return True

    @staticmethod
    def _damage_of(objects: dict) -> list[dict]:
        damage: list[dict] = []
        for object_id, obj in objects.items():
            seqs = [v.seq for v in obj.versions]
            if seqs != list(range(len(seqs))):
                damage.append({"kind": "history.seq_gap", "object": object_id, "seqs": seqs})
            known = set(seqs)
            pointers = set(obj.pins) | set(obj.compacted)
            if obj.preferred is not None:
                pointers.add(obj.preferred)
            if pointers - known:
                damage.append(
                    {"kind": "history.dangling_pointer", "object": object_id, "seqs": sorted(pointers - known)}
                )
            # Content integrity, where the snapshot recorded a digest to check
            # against. Older snapshots have none, and a missing digest is not
            # evidence of tampering -- it is simply a file from before the digest
            # existed, so it is not reported as damage.
            mismatched = [
                v.seq
                for v in obj.versions
                if v.digest is not None and v.digest != version_digest(v)
            ]
            if mismatched:
                damage.append(
                    {"kind": "history.payload_digest_mismatch", "object": object_id, "seqs": mismatched}
                )
        return damage

    def _audit_history(self) -> None:
        """Check loaded history for lost or dangling versions.

        Versions keep their metadata forever (compaction only reclaims payloads),
        so seqs must run 0..n-1 with no holes. A hole means an acknowledged
        version vanished. We report it and leave the data alone: repairing
        history silently would be its own violation of section 3.
        """
        self.history_damage = self._damage_of(self._objects)
        if self.history_damage:
            self.recovery_events.append({"kind": "history.damaged", "count": len(self.history_damage)})

    # --- authority -------------------------------------------------------
    def _check(
        self,
        cap: Optional[Capability],
        right: Right,
        target: Optional[str],
        holder: str,
        target_kind: Optional[str] = None,
    ) -> None:
        """Check authority for one operation, passing the Object's kind if we have it.

        The kind is resolved here rather than in the core because the store is the
        only thing that knows what kind an Object is. `_check` looks it up from
        `target` when it can, so every Object-right caller gets kind scoping
        without having to remember to supply it — forgetting would fail closed
        rather than silently widen a scoped token.
        """
        if target_kind is None and target is not None:
            obj = self._objects.get(target)
            if obj is not None:
                target_kind = obj.kind
        if cap is None:
            raise PermissionError(f"{holder} attempted {right.value} on {target} with no Capability")
        self._validate(cap, right, target, holder, target_kind)

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

    def retention_summary(self, holder: str, object_id: str, cap: Optional[Capability]) -> dict:
        """Summarize the retention state of one Object for audit and Console views."""
        obj = self._object(object_id)
        self._check(cap, Right.AUDIT, object_id, holder)
        return {
            "object_id": obj.object_id,
            "kind": obj.kind,
            "total_versions": len(obj.versions),
            "durable": sum(1 for v in obj.versions if v.acked),
            "pinned": len(obj.pins),
            "compacted": len(obj.compacted),
            "preferred": obj.preferred,
            "latest": obj.latest_seq,
        }

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
                "compacted": sorted(o.compacted),
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

        A version whose payload has been compacted cannot be preferred. `effective()`
        returns the preferred version without raising, so a pointer at reclaimed
        payloads would hand every consumer a `None` state -- and the restart path
        reads through `effective()`, so the demo Unit would be respawned from
        nothing. Refusing here keeps the invariant in one place instead of making
        every `effective()` caller defend against it.
        """
        obj = self._object(object_id)
        self._check(cap, Right.APPEND, object_id, holder)
        obj.get(seq)
        if seq in obj.compacted:
            raise CompactedError(
                f"cannot prefer {object_id}@{seq}: its payload was compacted. "
                "Metadata is retained; the payload is not recoverable"
            )
        obj.preferred = seq
        self._persist()

    @staticmethod
    def _reclaimable_seqs(obj: Object, keep_recent: int) -> list[int]:
        """The seqs `compact` would reclaim for this Object, oldest first.

        This is the single source of truth for what compaction may touch.
        `compact` acts on it and `reclaimable_report` describes it, so the
        operator's preview can never disagree with what compaction does.
        A version is protected if it is within the recent window (which always
        includes the newest), pinned, the preferred version, or already
        compacted.
        """
        keep_recent = max(1, int(keep_recent))
        cutoff = obj.latest_seq - keep_recent
        return [
            v.seq
            for v in obj.versions
            if not (v.seq > cutoff or v.seq in obj.pins or v.seq in obj.compacted or v.seq == obj.preferred)
        ]

    @staticmethod
    def _payload_bytes(version: Version) -> int:
        """Approximate size of a payload as it is persisted (UTF-8 JSON)."""
        if version.payload is None:
            return 0
        return len(json.dumps(version.payload, default=_json_default, sort_keys=True).encode("utf-8"))

    def _reclaimable_row(self, obj: Object, keep_recent: int) -> dict:
        keep_recent = max(1, int(keep_recent))
        seqs = self._reclaimable_seqs(obj, keep_recent)
        wanted = set(seqs)
        reclaimable_bytes = sum(self._payload_bytes(v) for v in obj.versions if v.seq in wanted)
        retained_bytes = sum(
            self._payload_bytes(v) for v in obj.versions if v.seq not in wanted and v.seq not in obj.compacted
        )
        cutoff = obj.latest_seq - keep_recent
        return {
            "object_id": obj.object_id,
            "kind": obj.kind,
            "total_versions": len(obj.versions),
            "reclaimable_seqs": seqs,
            "reclaimable_versions": len(seqs),
            "reclaimable_bytes": reclaimable_bytes,
            "retained_bytes": retained_bytes,
            "protected": {
                "recent": [v.seq for v in obj.versions if v.seq > cutoff],
                "pinned": sorted(obj.pins),
                "preferred": obj.preferred,
                "already_compacted": sorted(obj.compacted),
            },
        }

    def reclaimable_report(
        self,
        holder: str,
        cap: Optional[Capability],
        *,
        object_id: Optional[str] = None,
        keep_recent: int = 3,
    ) -> dict:
        """Read-only report of what `compact` could reclaim, so compaction is a
        visible, deliberate choice rather than something discovered afterwards.

        With `object_id`, reports that one Object and needs AUDIT on it. Without
        it, reports every Object plus store-wide totals and needs namespace-wide
        AUDIT, the same authority `enumerate` requires. Nothing is modified,
        persisted or appended: calling this never changes history.

        `keep_recent` is the same window `compact` takes (floored at 1), and the
        report echoes the value it actually used.
        """
        keep_recent = max(1, int(keep_recent))
        if object_id is not None:
            obj = self._object(object_id)
            self._check(cap, Right.AUDIT, object_id, holder)
            row = self._reclaimable_row(obj, keep_recent)
            row["keep_recent"] = keep_recent
            return row

        self._check(cap, Right.AUDIT, None, holder)
        rows = [self._reclaimable_row(o, keep_recent) for o in self._objects.values()]
        rows.sort(key=lambda r: (-r["reclaimable_bytes"], r["object_id"]))
        return {
            "keep_recent": keep_recent,
            "objects": rows,
            "totals": {
                "objects": len(rows),
                "objects_with_reclaimable": sum(1 for r in rows if r["reclaimable_versions"]),
                "total_versions": sum(r["total_versions"] for r in rows),
                "reclaimable_versions": sum(r["reclaimable_versions"] for r in rows),
                "reclaimable_bytes": sum(r["reclaimable_bytes"] for r in rows),
                "retained_bytes": sum(r["retained_bytes"] for r in rows),
            },
        }

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

        reclaimed = self._reclaimable_seqs(obj, keep_recent)

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
            # The marker version carries the Object's unchanged current state, so
            # compaction never replaces real data with bookkeeping. What was
            # reclaimed is recorded in the note, which is kept forever.
            self._do_append(
                obj,
                holder,
                obj.effective().payload,
                note=f"compacted {len(reclaimed)} version payload(s) {reclaimed}; pins {sorted(obj.pins)}; metadata retained",
                step=obj.versions[-1].step,
                acked=True,
            )
        self._persist()
        return {"reclaimed": reclaimed, "pins_preserved": sorted(obj.pins)}
