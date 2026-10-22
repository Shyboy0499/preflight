"""H-MEM (Meterless hierarchical memory), the two required services in Python.

Follows engines/hmem/HOW_TO_USE_H-MEM.md in github.com/Meterless/Meterless:

* Memory          - the canonical record (section 2); `source` and `confidence`
                    are mandatory, new writes carry `provenance`.
* TrustLedger     - append-only audit of every mutation and feedback (sections 7, 11).
* MemoryStoreService     - tiered persistence, CRUD, hydrate, feedback deltas (sections 3, 7).
* MemoryRetrievalService - the eight-signal hybrid ranker, top-N + threshold,
                           grouped reinjection format and a retrieval trace (section 6).

Preflight extension: `action` holds a reusable, check-gated action template
(action name, params, the checks it must pass, and where it applies). A
recalled template only changes which candidate is tried first; the gate still
runs every check.

Not implemented (out of scope for this app): mining, dreaming, sleep cycle,
conflict detection.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

MemoryType = Literal["personal", "factual", "preference", "general"]
MemoryLayer = Literal["short_term", "working", "long_term"]
FeedbackSignal = Literal["helpful", "not_helpful", "wrong"]

LAYER_WEIGHT = {"long_term": 1.0, "working": 0.8, "short_term": 0.6}
WEIGHT_PROFILE = "preflight-weights@2026-10"


def now_ms() -> int:
    return int(time.time() * 1000)


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


@dataclass
class Entity:
    name: str
    type: str


@dataclass
class Memory:
    id: str
    content: str
    type: MemoryType
    layer: MemoryLayer
    timestamp: int
    last_accessed: int
    access_count: int
    confidence: float
    source: str
    tags: list[str] = field(default_factory=list)
    domain: str | None = None
    namespace: str | None = None
    embedding: list[float] | None = None
    score: float | None = None
    entities: list[Entity] = field(default_factory=list)
    related_to: list[str] = field(default_factory=list)
    supersedes: str | None = None
    superseded_by: str | None = None
    derived_from: list[str] = field(default_factory=list)
    provenance: dict[str, Any] | None = None
    action: dict[str, Any] | None = None  # Preflight extension, see module docstring

    @classmethod
    def new(cls, content: str, *, type: MemoryType, layer: MemoryLayer, source: str, confidence: float, **kw) -> "Memory":
        ts = now_ms()
        return cls(
            id=f"mem_{uuid.uuid4().hex[:12]}",
            content=content,
            type=type,
            layer=layer,
            timestamp=ts,
            last_accessed=ts,
            access_count=0,
            confidence=confidence,
            source=source,
            **kw,
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Memory":
        d = dict(d)
        d["entities"] = [Entity(**e) for e in d.get("entities", [])]
        return cls(**d)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- embedding (local, no model call) ----------------------------------------

_TOKEN = re.compile(r"[a-z0-9_]+")
_STOP = {"the", "a", "an", "for", "to", "of", "on", "in", "and", "is", "needs", "need", "with", "at", "by"}


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


class HashingEmbedder:
    """Deterministic bag-of-words hashing embedder. Local and private; swap for a
    real embedding model without changing the store or ranker."""

    def __init__(self, dims: int = 256):
        self.dims = dims

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dims
        for tok in tokens(text):
            h = int(hashlib.sha256(tok.encode()).hexdigest(), 16)
            vec[h % self.dims] += 1.0 if (h >> 8) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return max(0.0, sum(x * y for x, y in zip(a, b)))


# --- trust ledger --------------------------------------------------------------


class TrustLedger:
    """Append-only JSONL. Never pruned or rewritten."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, action: str, memory_id: str, *, actor: str, reason: str = "", previous: Any = None, new: Any = None, **extra) -> None:
        entry = {
            "id": f"led_{uuid.uuid4().hex[:12]}",
            "ts": now_ms(),
            "action": action,
            "memory_id": memory_id,
            "actor": actor,
            "reason": reason,
            "previous_state": previous,
            "new_state": new,
            **extra,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def history(self, memory_id: str) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [e for e in map(json.loads, lines) if e["memory_id"] == memory_id]


# --- store ---------------------------------------------------------------------


class MemoryStoreService:
    """Tiered memory store persisted as one JSON file (long_term + working).
    short_term memories live only for the process."""

    def __init__(self, path: Path, ledger: TrustLedger, embedder: HashingEmbedder | None = None):
        self.path = path
        self.ledger = ledger
        self.embedder = embedder or HashingEmbedder()
        self._memories: dict[str, Memory] = {}

    def hydrate(self) -> list[Memory]:
        if self.path.exists():
            for raw in json.loads(self.path.read_text(encoding="utf-8")):
                m = Memory.from_dict(raw)
                self._memories[m.id] = m
        for m in self._memories.values():
            if not m.embedding:  # backfill missing embeddings
                m.embedding = self.embedder.embed(m.content)
        return self.all()

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        durable = [m.to_dict() for m in self._memories.values() if m.layer != "short_term"]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(durable, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def all(self) -> list[Memory]:
        return list(self._memories.values())

    def get(self, memory_id: str) -> Memory | None:
        return self._memories.get(memory_id)

    def add(self, memory: Memory, *, actor: str) -> Memory:
        if not memory.source:
            raise ValueError("H-MEM: a memory without a source is rejected")
        if memory.provenance is None:
            raise ValueError("H-MEM: new writes must carry provenance")
        memory.embedding = memory.embedding or self.embedder.embed(memory.content)
        self._memories[memory.id] = memory
        self.ledger.record("add", memory.id, actor=actor, new={"content": memory.content, "confidence": memory.confidence})
        self._persist()
        return memory

    def update(self, memory: Memory, *, actor: str, reason: str, previous: dict[str, Any]) -> None:
        self._memories[memory.id] = memory
        self.ledger.record(
            "update", memory.id, actor=actor, reason=reason, previous=previous,
            new={"confidence": memory.confidence, "access_count": memory.access_count, "tags": memory.tags},
        )
        self._persist()

    def apply_feedback(self, memory_id: str, signal: FeedbackSignal, *, actor: str, trace_id: str, reason: str = "") -> None:
        m = self._memories[memory_id]
        previous = {"confidence": m.confidence, "access_count": m.access_count, "tags": list(m.tags)}
        if signal == "helpful":
            m.access_count += 1
            m.confidence = clamp01(m.confidence + 0.05)
        elif signal == "not_helpful":
            m.confidence = clamp01(m.confidence - 0.03)
        elif signal == "wrong":
            m.confidence = clamp01(m.confidence - 0.20)
            m.tags = sorted(set(m.tags) | {"review"})
        self.ledger.record("feedback", memory_id, actor=actor, reason=reason, signal=signal, trace_id=trace_id)
        self.update(m, actor=actor, reason=f"feedback:{signal}", previous=previous)

    def touch(self, memory_ids: list[str]) -> None:
        ts = now_ms()
        for mid in memory_ids:
            if mid in self._memories:
                self._memories[mid].last_accessed = ts
        if memory_ids:
            self._persist()


# --- retrieval -------------------------------------------------------------------


@dataclass
class RetrievalQuery:
    text: str
    tags: list[str] = field(default_factory=list)
    domain: str | None = None
    entities: list[Entity] = field(default_factory=list)
    top_n: int = 5
    threshold: float = 0.35


@dataclass
class RetrievalResult:
    memories: list[Memory]
    trace: dict[str, Any]


class MemoryRetrievalService:
    """Eight-signal hybrid ranker from HOW_TO_USE_H-MEM.md section 6."""

    def __init__(self, store: MemoryStoreService):
        self.store = store

    @staticmethod
    def _jaccard(a: set[str], b: set[str]) -> float:
        return len(a & b) / len(a | b) if a and b else 0.0

    def score(self, q: RetrievalQuery, q_emb: list[float], m: Memory) -> tuple[float, dict[str, float]]:
        q_keywords = set(tokens(q.text)[:8])
        q_entities = {e.name.lower() for e in q.entities}
        days_idle = max(0.0, (now_ms() - m.last_accessed) / 86_400_000)
        signals = {
            "semantic": 0.35 * cosine(q_emb, m.embedding or []),
            "keyword": 0.20 * self._jaccard(q_keywords, set(tokens(m.content))),
            "tags": 0.10 * self._jaccard(set(q.tags), set(m.tags)),
            "domain": 0.10 * (1.0 if q.domain and m.domain == q.domain else 0.0),
            "entities": 0.15 * self._jaccard(q_entities, {e.name.lower() for e in m.entities}),
            "layer": 0.05 * LAYER_WEIGHT[m.layer],
            "recency": 0.05 * math.exp(-days_idle / 14),
            "superseded": -0.20 * (1.0 if m.superseded_by else 0.0),
        }
        return clamp01(sum(signals.values())) * m.confidence, signals

    def query(self, q: RetrievalQuery) -> RetrievalResult:
        q_emb = self.store.embedder.embed(q.text)
        scored = []
        for m in self.store.all():
            s, signals = self.score(q, q_emb, m)
            scored.append((s, m, signals))
        scored.sort(key=lambda t: t[0], reverse=True)
        chosen = [(s, m, sig) for s, m, sig in scored if s >= q.threshold][: q.top_n]
        for s, m, _ in chosen:
            m.score = round(s, 4)
        self.store.touch([m.id for _, m, _ in chosen])
        trace = {
            "trace_id": f"trace_{uuid.uuid4().hex[:12]}",
            "weight_profile": WEIGHT_PROFILE,
            "query": q.text,
            "tags": q.tags,
            "domain": q.domain,
            "top_n": q.top_n,
            "threshold": q.threshold,
            "considered": len(scored),
            "returned": [
                {"id": m.id, "score": round(s, 4), "signals": {k: round(v, 4) for k, v in sig.items()}, "content": m.content}
                for s, m, sig in chosen
            ],
        }
        return RetrievalResult([m for _, m, _ in chosen], trace)


def format_reinjection(memories: list[Memory]) -> str:
    """Grouped by domain with layer markers and entities (section 6)."""
    lines = []
    for m in sorted(memories, key=lambda m: (m.domain or "", -(m.score or 0))):
        ents = ", ".join(e.name for e in m.entities)
        lines.append(f"[{m.domain or 'general'}] ({m.layer}) {m.content}" + (f" {{entities: {ents}}}" if ents else ""))
    return "\n".join(lines)
