"""Local ranking under quotas (§4.3). Jev's J1 rerank plugs in here (lane CTXQ-JW).

THE SCORE is the design's weighted sum, with the weights fixed here and tuned
only by the offline replay (§6), never per call:

    local = w_d · 1/(1+hops)        graph distance from the seeds
          + w_c · edge confidence   lsp > ast > import > naming
          + w_h · co-change support layer H (and how often a doc names the path)
          + w_t · BM25(brief)       layer S terms, normalised to the best hit
          + w_r · recency           0.5 ** (age_days / 14): half-life 14 days

and a row's score is `(PRIOR[kind] + local) · EMPHASIS[type][kind]`. The
prior is what a row of that kind is worth with no edge to measure (a house
rule naming the file has no hop count); the emphasis is §4.5's "retrieval
emphasis" per task type -- a design pack is doc-heavy, a fix pack leans on
the red tests' code and earlier fixes.

QUOTAS, so one kind cannot crowd out the rest: at least 3 tests and 2 guards
when they exist, at most 5 docs and 5 similar changes (design: 12 docs).
In-flight and cannot-push rows are not ranked at all: they are always shown.
"""

from __future__ import annotations

from typing import Iterable

from .retrieve import ALWAYS, Candidate

WEIGHTS = {"distance": 1.0, "confidence": 0.6, "cochange": 0.5, "bm25": 0.4, "recency": 0.3}
HALF_LIFE_DAYS = 14.0
#: Co-change support counts up to this many; more is not more evidence.
SUPPORT_CAP = 5
PRIOR = {"symbol": 0.6, "caller": 0.3, "callee": 0.1, "test": 0.4, "guard": 0.8,
         "rule": 0.9, "doc": 0.3, "similar_pr": 0.3}
_ALL = ("symbol", "caller", "callee", "test", "guard", "rule", "doc", "similar_pr")
EMPHASIS: dict[str, dict[str, float]] = {
    "implement": {},
    "review": {"caller": 1.3, "test": 1.2, "callee": 0.6},
    "fix": {"similar_pr": 1.6, "symbol": 1.2, "caller": 0.7},
    "test": {"test": 1.4, "callee": 0.6, "doc": 0.7},
    "design": {"doc": 1.8, "rule": 1.3, "caller": 0.6, "callee": 0.5, "test": 0.6},
    "plan": {"similar_pr": 1.3, "doc": 1.1},
}
_BASE_QUOTA = {"min": {"test": 3, "guard": 2}, "max": {"doc": 5, "similar_pr": 5}}
QUOTAS: dict[str, dict[str, dict[str, int]]] = {
    kind: {"min": dict(_BASE_QUOTA["min"]), "max": dict(_BASE_QUOTA["max"])}
    for kind in EMPHASIS
}
QUOTAS["design"]["max"]["doc"] = 12
QUOTAS["design"]["min"]["test"] = 0
QUOTAS["fix"]["max"]["similar_pr"] = 8


def local_score(cand: Candidate) -> float:
    total = 0.0
    if cand.hops is not None:
        total += WEIGHTS["distance"] / (1 + max(cand.hops, 0))
    if cand.confidence is not None:
        total += WEIGHTS["confidence"] * max(min(cand.confidence, 1.0), 0.0)
    if cand.support:
        total += WEIGHTS["cochange"] * min(cand.support, SUPPORT_CAP) / SUPPORT_CAP
    if cand.bm25:
        total += WEIGHTS["bm25"] * cand.bm25
    if cand.age_days is not None:
        total += WEIGHTS["recency"] * 0.5 ** (max(cand.age_days, 0.0) / HALF_LIFE_DAYS)
    return total


def score(cand: Candidate, task_type: str) -> float:
    emphasis = EMPHASIS.get(task_type, {}).get(cand.kind, 1.0)
    return round((PRIOR.get(cand.kind, 0.0) + local_score(cand)) * emphasis, 6)


def ordered(pool: Iterable[Candidate]) -> list[Candidate]:
    """Best first; ties broken by kind and ref, so two builds agree."""
    return sorted(pool, key=lambda c: (-c.score, _ALL.index(c.kind) if c.kind in _ALL
                                       else len(_ALL), c.ref))


def select(pool: Iterable[Candidate], task_type: str,
           budget_rows: int | None = None) -> list[Candidate]:
    """The ranked rows in the order the budget should take them.

    Each kind's minimum comes first (its best rows), then everything else best
    first, skipping a kind at its maximum. `budget_rows` stops it there; the
    renderer stops it at the byte budget instead.
    """
    quota = QUOTAS.get(task_type, QUOTAS["implement"])
    rows = ordered(c for c in pool if c.kind not in ALWAYS)
    chosen: list[Candidate] = []
    taken: set[tuple[str, str]] = set()
    counts: dict[str, int] = {}

    def take(cand: Candidate) -> bool:
        if budget_rows is not None and len(chosen) >= budget_rows:
            return False
        chosen.append(cand)
        taken.add(cand.key())
        counts[cand.kind] = counts.get(cand.kind, 0) + 1
        return True

    for kind, minimum in quota["min"].items():
        for cand in [c for c in rows if c.kind == kind][:minimum]:
            take(cand)
    for cand in rows:
        if cand.key() in taken:
            continue
        if counts.get(cand.kind, 0) >= quota["max"].get(cand.kind, 1 << 30):
            continue
        if not take(cand):
            break
    return chosen
