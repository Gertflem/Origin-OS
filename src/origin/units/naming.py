"""Naming and Intent resolution as an ordinary Unit (step 1.9).

Constitution section 6: both exact names and rich descriptive phrases are
first-class; ambiguity is resolved by presenting ranked suggestions for
confirmation; naming and full intent resolution are handled by ordinary Units
that can evolve; and full intent resolution is supported from early stages.

Two things follow from "ordinary Unit" that are worth being explicit about,
because they are the point of putting this here rather than in the core:

The naming scheme can be *wrong* and be fixed. Ranking is a heuristic in
`score()`; when it ranks badly, someone edits this Unit and restarts it, and no
invariant is touched. A Nucleus that resolved names would have to be right
forever.

Resolving a name requires a RESOLVE Capability. Section 5 says discovery of
names without a relevant Capability is impossible, so this Unit has no unguarded
lookup and no `list` you can call empty-handed. It will happily tell you that
"the bright beach photo" is ambiguous — but only if you were already allowed to
ask.

Intent parsing here is deliberately rule-based rather than learned. It implements
the *shape* of intent resolution — verb recognition,
reference resolution, capability requirement discovery, ambiguity escalation —
and a rule-based parser makes every one of those steps inspectable, which is
what section 8 demands. Advanced intent intelligence is a later parallel track.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from ..capability import CapabilityError, Right
from ..message import Message
from ..unit import UnitContext, is_answer, unit_type


@dataclass
class Binding:
    """One name pointing at one target."""

    name: str
    target: str
    kind: str  # "object" | "contact" | "unit"
    description: str = ""
    aliases: tuple[str, ...] = ()

    def describe(self) -> dict:
        return {
            "name": self.name,
            "target": self.target,
            "kind": self.kind,
            "description": self.description,
            "aliases": list(self.aliases),
        }


@dataclass
class IntentVerb:
    """A thing the system knows how to do, and what authority it costs.

    `rights` is what makes intent-time capability proposals possible: the moment
    a phrase is parsed into actions, the authority those actions need is already
    known, so the Console can show the human a proposal before anything runs.
    """

    action: str
    unit_kind: str
    verb: str
    rights: tuple[Right, ...]
    synonyms: tuple[str, ...] = ()
    #: True when the action needs a recipient as well as a target.
    takes_recipient: bool = False
    #: True when the action refers to a specific version, as in "go back to
    #: version 2". The seq is parsed out of the clause rather than being a second
    #: target, because it qualifies the target rather than naming another Object.
    takes_seq: bool = False
    describe_result: bool = False

    @property
    def words(self) -> frozenset[str]:
        return frozenset({self.action, *self.synonyms})


INTENT_VERBS: tuple[IntentVerb, ...] = (
    IntentVerb(
        action="brighten",
        unit_kind="photo",
        verb="brighten",
        rights=(Right.READ, Right.APPEND),
        synonyms=("brighter", "bright", "lighten", "lighter", "make brighter"),
    ),
    IntentVerb(
        action="resize",
        unit_kind="photo",
        verb="resize",
        rights=(Right.READ, Right.APPEND),
        synonyms=("smaller", "larger", "scale", "shrink", "enlarge"),
    ),
    IntentVerb(
        action="send",
        unit_kind="mail",
        verb="send",
        # Mail delivery appends to the recipient's mailbox Object and may read the
        # attachment, so its authority cost is APPEND on the mailbox plus READ on
        # the object — not SEND, which is the right to route a Message at all and
        # is granted by the Nucleus rather than proposed at intent time.
        rights=(Right.READ, Right.APPEND),
        synonyms=("mail", "deliver", "forward", "email"),
        takes_recipient=True,
    ),
    IntentVerb(
        action="count",
        unit_kind="counter",
        verb="count",
        rights=(Right.READ, Right.APPEND),
        synonyms=("tally", "increment", "add one"),
    ),
    IntentVerb(
        action="show",
        unit_kind="",
        verb="show",
        rights=(Right.READ,),
        synonyms=("display", "read", "open", "look at", "what is"),
        describe_result=True,
    ),
    IntentVerb(
        action="undo",
        unit_kind="",
        verb="undo",
        # Section 3: history is the mechanism, so reversing is a matter of reading an
        # earlier version. HISTORY is needed to turn "the last version" into a
        # concrete seq, and it is quoted to the human rather than quietly acquired.
        # No authority over the *current* state is needed, which is why "undo" cannot
        # quietly become a destructive operation.
        rights=(Right.READ, Right.HISTORY),
        # "restore" is deliberately absent: it is a different action with a
        # different effect (it moves the preferred pointer rather than reading an
        # earlier version), and listing it here made "restore version 0" report
        # itself as an undo.
        synonyms=("revert", "roll back", "rollback", "go back to", "undo"),
        takes_seq=True,
        describe_result=True,
    ),
    IntentVerb(
        action="history",
        unit_kind="",
        verb="history",
        rights=(Right.HISTORY,),
        synonyms=("versions", "revision history", "what changed", "timeline"),
        describe_result=True,
    ),
    IntentVerb(
        action="restore",
        unit_kind="",
        verb="restore",
        # Restoring marks an earlier version preferred. That is the same mechanism
        # the Improver uses to promote a repaired Unit, which is the point: recovery
        # is not a special path, it is the versioned substrate working normally.
        #
        # HISTORY resolves the version reference; APPEND is what `prefer` gates on,
        # not because restoring writes a version -- nothing is written. Preferring
        # only moves a pointer, and the superseded version stays addressable by seq,
        # so this is reversible in exactly the way an Improver's improvement is.
        rights=(Right.READ, Right.HISTORY, Right.APPEND),
        synonyms=("make current", "set as current", "prefer"),
        takes_seq=True,
        describe_result=True,
    ),
)

#: Clause separators. Splitting intent into a sequence of actions is what lets
#: "make this photo brighter and send it to David" become two steps with two
#: different authority requirements.
CLAUSE_SPLIT = re.compile(r"\b(?:and then|and|then|,|;)\b")

#: Deictic references — resolved against the Console's current focus rather than
#: against the namespace, because "this photo" means the one we were just
#: talking about, not one named "this".
DEICTIC = frozenset({"this", "that", "it", "these", "those", "same", "the same"})


def is_deictic_reference(phrase: str) -> bool:
    """Recognise focus-based references like "the same photo" or "that picture"."""
    cleaned = [w for w in tokens(phrase) if w not in {"the", "a", "an", "my", "our"}]
    if not cleaned:
        return True
    return cleaned[0] in DEICTIC or "same" in cleaned or " ".join(cleaned[:2]) in DEICTIC

WORDS = re.compile(r"[a-z0-9']+")

#: A trailing numeric parameter: "by 20", "to 0.5", "to 50%".
MODIFIER = re.compile(r"\b(?:by|to|at)\s+(-?\d+(?:\.\d+)?)\s*(%|percent)?\s*$")

#: An explicit version reference: "version 2", "seq 0", "the last version",
#: "the version before that". Ordinals and "last" are resolved against the target
#: Object's length at execution time, not here, because only the store knows how
#: many versions exist.
SEQ_REFERENCE = re.compile(r"\b(?:version|seq|revision)\s+(?:number\s+)?(-?\d+)\b")
ORDINAL = re.compile(r"\b(?:the\s+)?(first|second|third|last|latest|previous|prior)\b")


def normalize_phrase(text: str) -> str:
    """Lowercase and strip punctuation so natural language matches names reliably."""
    value = text.strip().lower()
    value = value.replace("-", " ")
    value = re.sub(r"[.,!?;:]+$", "", value)
    value = re.sub(r"[^a-z0-9\s']+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def tokens(text: str) -> list[str]:
    return WORDS.findall(normalize_phrase(text))


def extract_modifier(clause: str) -> tuple[float | None, str]:
    """Pull a trailing numeric parameter out of a clause.

    "Brighten the photo by 20" carries an amount the verb table cannot know
    about, and leaving it in the target phrase turns it into a spurious
    ambiguity — nothing in the namespace is named "by 20". Extracted before the
    recipient because "resize to 0.5" would otherwise resolve "0.5" as a contact.
    """
    clean = normalize_phrase(clause)
    m = MODIFIER.search(clean)
    if not m:
        return None, clause
    value = float(m.group(1))
    if m.group(2):
        value /= 100.0
    return value, clean[: m.start()].strip()


def score(binding: Binding, phrase: str) -> float:
    """Rank how well a Binding matches a descriptive phrase.

    Layered so that precision wins: an exact name beats an alias, an alias beats
    a prefix, a prefix beats substring containment, and containment beats fuzzy
    similarity. Fuzzy similarity alone never scores high enough to be treated as
    an exact match, which is what keeps the Console from confidently acting on a
    guess — it must fall through to ranked suggestions instead.
    """
    p = normalize_phrase(phrase)
    name = normalize_phrase(binding.name)
    aliases = {normalize_phrase(a) for a in binding.aliases}
    if p == name:
        return 1.0
    if p in aliases:
        return 0.95

    # Strip articles so "the beach photo" finds a binding named "beach photo".
    stripped = re.sub(r"^(?:the|a|an|my|our)\s+", "", p)
    if stripped == name:
        return 0.93
    if name.startswith(stripped) or stripped.startswith(name):
        return 0.80

    haystack = normalize_phrase(" ".join([binding.name, binding.description, *binding.aliases]))
    if stripped and stripped in haystack:
        return 0.70

    # Word overlap: "bright beach photo" should find "beach photo (bright, sunny)".
    phrase_words = set(tokens(stripped))
    hay_words = set(tokens(haystack))
    if phrase_words and hay_words:
        overlap = len(phrase_words & hay_words) / len(phrase_words | hay_words)
        if overlap > 0:
            return 0.40 + 0.25 * overlap

    fuzzy = SequenceMatcher(None, stripped, name).ratio()
    return round(0.35 * fuzzy, 4)


@dataclass
class ParseStep:
    """One action in a resolved intent."""

    action: str
    verb: str
    unit_kind: str
    rights: tuple[str, ...]
    target_phrase: str
    target: str | None = None
    target_score: float = 0.0
    recipient_phrase: str | None = None
    recipient: str | None = None
    recipient_score: float = 0.0
    modifier: float | None = None
    #: "version 2", "last", "previous" — resolved to a concrete seq at execution
    #: time by whichever Unit holds the Object, because only it knows the history.
    seq_reference: str | None = None
    describe_result: bool = False
    candidates: list[dict] = field(default_factory=list)

    @property
    def ambiguous(self) -> bool:
        return self.target is None or bool(self.candidates)

    def describe(self) -> dict:
        return {
            "action": self.action,
            "verb": self.verb,
            "unit_kind": self.unit_kind,
            "rights": list(self.rights),
            "target_phrase": self.target_phrase,
            "target": self.target,
            "target_score": self.target_score,
            "recipient_phrase": self.recipient_phrase,
            "recipient": self.recipient,
            "modifier": self.modifier,
            "seq_reference": self.seq_reference,
            "candidates": self.candidates,
        }


def match_verb(words: list[str]) -> IntentVerb | None:
    """Find the action a clause is asking for.

    Longest synonym first, so "make brighter" wins over "brighter" and "add one"
    wins over "add". Without that ordering, multi-word synonyms would never fire
    because their first word would match a shorter entry.
    """
    joined = " ".join(words)
    best: IntentVerb | None = None
    best_len = 0
    for iv in INTENT_VERBS:
        for syn in sorted(iv.words, key=len, reverse=True):
            if re.search(rf"\b{re.escape(syn)}\b", joined) and len(syn) > best_len:
                best, best_len = iv, len(syn)
    return best


def extract_recipient(clause: str) -> tuple[str | None, str]:
    """Pull "to David" out of a clause, returning (recipient, remaining text)."""
    clean = normalize_phrase(clause)
    m = re.search(r"\b(?:to|for)\s+([a-z0-9' ]+)$", clean)
    if not m:
        return None, clause
    return m.group(1).strip(), clean[: m.start()].strip()


def strip_verb(clause: str, iv: IntentVerb) -> str:
    """Remove the action words and filler, leaving the target phrase."""
    out = normalize_phrase(clause)
    for syn in sorted(iv.words, key=len, reverse=True):
        out = re.sub(rf"\b{re.escape(syn)}\b", " ", out)
    out = re.sub(r"\b(?:make|please|could you|can you|i want to|turn|get|set)\b", " ", out)
    out = re.sub(r"\b(?:a bit|slightly|very|much|some)\b", " ", out)
    # Carry-overs from the sequence verbs. "Go back to version 2 of the photo"
    # strips to "go back", and leaving that in turns the target phrase into a
    # phrase that matches nothing -- the intent then fails to resolve rather than
    # resolving wrongly, which is better but still a poor answer. These are the
    # words undo/restore are built from, so they have to go with them.
    out = re.sub(r"\b(?:go|back|undo|revert|restore|roll)\b", " ", out)
    out = re.sub(r"\b(?:what|changed|in|of|make|current)\b", " ", out)
    return re.sub(r"\s+", " ", out).strip()


def extract_seq(clause: str) -> tuple[str | None, str]:
    """Pull a version reference out of a clause, returning (reference, remainder).

    "Go back to version 2 of the beach photo" has to shed "version 2" before the
    phrase is resolved, or the target phrase becomes "version 2 of the beach photo"
    and matches nothing. The reference is kept as a *string* because ordinals like
    "the last version" cannot be turned into a seq until the store has said how many
    versions the Object has.
    """
    clean = normalize_phrase(clause)

    m = SEQ_REFERENCE.search(clean)
    if m:
        return m.group(1), (clean[: m.start()] + " " + clean[m.end() :]).strip()

    m = ORDINAL.search(clean)
    if m:
        word = m.group(1)
        return word, (clean[: m.start()] + " " + clean[m.end() :]).strip()

    return None, clean


@unit_type("naming")
def naming_handler(ctx: UnitContext, msg: Message) -> None:
    bindings: dict[str, Binding] = ctx.mem.setdefault("bindings", {})
    payload = msg.payload or {}
    cap = msg.caps[0] if msg.caps else None
    # A single bound method of the core, placed in this arena at bootstrap. It is
    # an unforgeable reference to exactly one operation — "is this token valid
    # for this right?" — and can neither mint nor revoke nor route. Narrow
    # function references like this are capabilities in the literal
    # object-capability sense, which is why a Unit can hold one without holding
    # any authority over the system.
    validate = ctx.mem["validator"]

    def demand(right: Right, target: str | None = None) -> None:
        if cap is None:
            raise CapabilityError(f"{msg.verb} requires a {right.value.upper()} Capability")
        validate(cap, right, target, msg.sender)

    def resolve_phrase(phrase: str, kind: str | None) -> tuple[str | None, float, list[dict]]:
        """Return (target, score, ranked_candidates).

        A score of 1.0 or 0.95 is treated as unambiguous and resolved outright.
        Anything lower returns the ranked list and no target, because section 6
        says ambiguity is resolved by presenting suggestions for confirmation —
        not by picking the top guess and hoping.
        """
        ranked = sorted(
            (
                (score(b, phrase), b)
                for b in bindings.values()
                if kind is None or b.kind == kind
            ),
            key=lambda t: t[0],
            reverse=True,
        )
        ranked = [(s, b) for s, b in ranked if s > 0.15][:5]
        if not ranked:
            return None, 0.0, []
        top_score, top = ranked[0]
        if top_score >= 0.95:
            return top.target, top_score, []
        # Two near-equal matches are ambiguous even if the top one scores well.
        if len(ranked) > 1 and ranked[1][0] >= top_score - 0.02:
            return None, top_score, [
                {"name": b.name, "target": b.target, "kind": b.kind, "score": round(s, 3), "description": b.description}
                for s, b in ranked
            ]
        if top_score >= 0.80:
            return top.target, top_score, []
        return None, top_score, [
            {"name": b.name, "target": b.target, "kind": b.kind, "score": round(s, 3), "description": b.description}
            for s, b in ranked
        ]

    try:
        if msg.verb == "name.bind":
            demand(Right.BIND, payload.get("name"))
            b = Binding(
                name=payload["name"],
                target=payload["target"],
                kind=payload.get("kind", "object"),
                description=payload.get("description", ""),
                aliases=tuple(payload.get("aliases", ())),
            )
            bindings[b.name.lower()] = b
            ctx.respond(msg, "name.bound", b.describe())

        elif msg.verb == "name.unbind":
            demand(Right.BIND, payload.get("name"))
            removed = bindings.pop(payload["name"].lower(), None)
            ctx.respond(msg, "name.unbound", {"name": payload["name"], "existed": removed is not None})

        elif msg.verb == "name.resolve":
            demand(Right.RESOLVE)
            target, s, cands = resolve_phrase(payload.get("phrase", ""), payload.get("kind"))
            ctx.respond(
                msg,
                "name.resolved",
                {
                    "phrase": payload.get("phrase", ""),
                    "target": target,
                    "score": round(s, 3),
                    "ambiguous": target is None,
                    "candidates": cands,
                },
            )

        elif msg.verb == "name.list":
            demand(Right.RESOLVE)
            ctx.respond(
                msg, "name.list", {"bindings": [b.describe() for b in bindings.values()]}
            )

        elif msg.verb == "intent.parse":
            demand(Right.RESOLVE)
            ctx.respond(msg, "intent.plan", _parse(payload.get("text", ""), payload.get("focus"), resolve_phrase))

        else:
            if not is_answer(msg.verb):
                ctx.respond(msg, "name.error", {"reason": f"unknown verb {msg.verb!r}"})

    except CapabilityError as exc:
        ctx.respond(msg, "name.denied", {"verb": msg.verb, "reason": str(exc)})


def _parse(text: str, focus: dict | None, resolve_phrase) -> dict:
    """Turn a phrase into a ranked, authority-costed plan.

    Returns either `steps` (fully resolved, ready to execute) or `ambiguities`
    (something needs the human to choose). It never silently picks a guess.
    """
    focus = focus or {}
    clauses = [c.strip() for c in CLAUSE_SPLIT.split(text.lower()) if c and c.strip()]
    steps: list[dict] = []
    ambiguities: list[dict] = []
    unknown: list[str] = []

    for clause in clauses:
        words = tokens(clause)
        if not words:
            continue
        iv = match_verb(words)
        if iv is None:
            unknown.append(clause)
            continue

        modifier, remainder = extract_modifier(clause)
        # Version references are pulled before everything else, and only for verbs
        # that take one. "go back to version 2 of the beach photo" has to shed the
        # reference first, or the target phrase matches nothing and "to ..." is
        # misread as a recipient.
        seq_reference = None
        if iv.takes_seq:
            seq_reference, remainder = extract_seq(remainder)
        # Only pull a recipient for verbs that take one. Otherwise "go back to
        # version 2 of the beach photo" has "of the beach photo" stripped off as a
        # recipient and the target ends up empty -- a confidently wrong parse of a
        # sentence the human wrote perfectly clearly.
        recipient_phrase = None
        if iv.takes_recipient:
            recipient_phrase, remainder = extract_recipient(remainder)
        target_phrase = strip_verb(remainder, iv)
        # A leftover "version" with the number already lifted out is noise, not a
        # target: "restore the version of the tally" means the tally.
        if iv.takes_seq:
            # "undo to version 9 of X" leaves a dangling "to" once the reference
            # is lifted, and "to X" would then score as a prefix match against
            # almost nothing -- or resolve to a contact, since "to" is what marks a
            # recipient everywhere else.
            target_phrase = re.sub(r"^\s*(?:to|for)\s+", "", target_phrase)
            target_phrase = re.sub(r"\b(?:version|revision|seq)\b", " ", target_phrase)
            # Removing the word can leave doubled or leading articles behind,
            # because the filler pass ran before this one.
            target_phrase = re.sub(r"\b(?:the|a|an)\s+(?:the|a|an)\b", "the", target_phrase)
            target_phrase = re.sub(r"^(?:the|a|an)\s+(?:the|a|an)\s+", "the ", target_phrase)
            target_phrase = re.sub(r"\s+", " ", target_phrase).strip()

        step = ParseStep(
            action=iv.action,
            verb=iv.verb,
            unit_kind=iv.unit_kind,
            rights=tuple(r.value for r in iv.rights),
            target_phrase=target_phrase,
            recipient_phrase=recipient_phrase,
            modifier=modifier,
            seq_reference=seq_reference,
            describe_result=iv.describe_result,
        )

        # Deictic references resolve against the conversation focus, not the
        # namespace. "make this photo brighter" only means something in context,
        # and an empty target phrase means "the thing we were just discussing".
        tw = tokens(target_phrase)
        if focus.get("object_id") and (not tw or is_deictic_reference(target_phrase)):
            step.target = focus["object_id"]
            step.target_score = 1.0
            step.target_phrase = target_phrase or "this"
        elif target_phrase:
            t, s, cands = resolve_phrase(target_phrase, "object")
            step.target, step.target_score, step.candidates = t, s, cands
            if t is None:
                ambiguities.append({"clause": clause, "role": "target", "phrase": target_phrase, "candidates": cands})

        if recipient_phrase:
            rw = tokens(recipient_phrase)
            if focus.get("recipient") and (not rw or rw[0] in DEICTIC):
                step.recipient = focus["recipient"]
                step.recipient_score = 1.0
            else:
                # Contacts are their own binding kind. Resolving a recipient
                # against Objects would happily return a photo for "to David",
                # which is the kind of confident wrong answer section 6 forbids.
                r, rs, rcands = resolve_phrase(recipient_phrase, "contact")
                step.recipient, step.recipient_score = r, rs
                if r is None:
                    ambiguities.append(
                        {"clause": clause, "role": "recipient", "phrase": recipient_phrase, "candidates": rcands}
                    )

        steps.append(step.describe())

    return {
        "text": text,
        "steps": steps,
        "ambiguities": ambiguities,
        "unrecognised": unknown,
        "resolved": bool(steps) and not ambiguities,
    }
