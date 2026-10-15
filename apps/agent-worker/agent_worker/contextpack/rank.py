"""Local ranking under quotas, then Jev's J1 rerank when the workspace opted in (§4.3).

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

J1 (lane CTXQ-JW, `rerank`). When this attempt has a Jev client, the best
`J1_CANDIDATES` locally ranked rows go to Jev in ONE call, one `noul` each
("would an engineer doing `task` need to read this?"), and each sent row's
score becomes `JEV_WEIGHT · local/best + (1 - JEV_WEIGHT) · p`. A row Jev puts
under `JEV_DEMOTE_BELOW` goes below the budget line -- after every other row,
so it is paged, never deleted (jev-browser's bias-to-keep). In-flight and
cannot-push rows are never sent and never touched: they are outside the
ranking. ADVISORY AND ALL-OR-NOTHING: a call that is skipped, fails, times
out or answers malformed changes nothing, so the pack is byte for byte the
one the local-only run writes.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .retrieve import ALWAYS, Candidate

WEIGHTS = {"distance": 1.0, "confidence": 0.6, "cochange": 0.5, "bm25": 0.4, "recency": 0.3}
HALF_LIFE_DAYS = 14.0
#: Co-change support counts up to this many; more is not more evidence.
SUPPORT_CAP = 5
PRIOR = {"symbol": 0.6, "caller": 0.3, "callee": 0.1, "test": 0.4, "guard": 0.8,
         "rule": 0.9, "doc": 0.6, "similar_pr": 0.3}
_ALL = ("symbol", "caller", "callee", "test", "guard", "rule", "doc", "similar_pr")
EMPHASIS: dict[str, dict[str, float]] = {
    "implement": {},
    "review": {"caller": 1.3, "test": 1.2, "callee": 0.6},
    "fix": {"similar_pr": 1.6, "symbol": 1.2, "caller": 0.7},
    "test": {"test": 1.4, "callee": 0.6, "doc": 0.7},
    "design": {"doc": 1.8, "rule": 1.3, "caller": 0.6, "callee": 0.5, "test": 0.6},
    "plan": {"similar_pr": 1.3, "doc": 1.1},
}
#: `symbol` is capped because a seed file's own symbols are what the agent
#: opens first, and `test` because a hub file is covered by hundreds: the
#: pre-publish gate (CTXQ-G) runs the graph-selected tests whatever is shown,
#: and `tests_for` pages the rest. Measured on this repository's own snapshot
#: (2026-10-14, a step touching `agent_worker/children.py`): uncapped, 65
#: test rows and 62 symbol rows filled the 16 KiB and no doc was shown.
_BASE_QUOTA = {"min": {"test": 3, "guard": 2},
               "max": {"doc": 5, "similar_pr": 5, "symbol": 10, "test": 20}}
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
    renderer stops it at the byte budget instead. A row J1 demoted
    (`JEV_DEMOTED`) fills no minimum and comes after every other row.
    """
    quota = QUOTAS.get(task_type, QUOTAS["implement"])
    ranked = ordered(c for c in pool if c.kind not in ALWAYS)
    rows = [c for c in ranked if not c.extra.get(JEV_DEMOTED)] + \
        [c for c in ranked if c.extra.get(JEV_DEMOTED)]
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
        for cand in [c for c in rows if c.kind == kind and not c.extra.get(JEV_DEMOTED)][:minimum]:
            take(cand)
    for cand in rows:
        if cand.key() in taken:
            continue
        if counts.get(cand.kind, 0) >= quota["max"].get(cand.kind, 1 << 30):
            continue
        if not take(cand):
            break
    return chosen


# -- J1: Jev's rerank (§4.3, §5.2) ---------------------------------------------

#: §4.3: "the top 60 local candidates go to Jev".
J1_CANDIDATES = 60
#: §4.3: the final order is `0.5·local_rank_score + 0.5·p_yes`.
JEV_WEIGHT = 0.5
#: §4.3: a row Jev scores under this drops below the budget line (paged, kept).
JEV_DEMOTE_BELOW = 0.15
#: The keys J1 adds to a row it answered for (`Candidate.extra`, so `as_row`
#: carries them into pack.json). A row Jev was not asked about has neither.
JEV_P = "jev_p"
JEV_DEMOTED = "jev_demoted"


def _symbol(cand: Candidate) -> str | None:
    return cand.ref.split("#", 1)[1] if "#" in cand.ref else None


def j1_row(cand: Candidate) -> dict[str, Any]:
    """What J1 sends for one row: names, the reason, and the row's evidence as
    its excerpt. `judge.sanitize` then applies §5.4 to every string."""
    row: dict[str, Any] = {"kind": cand.kind, "path": cand.path, "why": cand.why}
    symbol = _symbol(cand)
    if symbol:
        row["symbol"] = symbol
    if cand.evidence:
        row["excerpt"] = cand.evidence
    return row


def rerank(pool: Iterable[Candidate], task_type: str, brief: str, judge: Any,
           ) -> Mapping[str, Any] | None:
    """J1 over the pool, in place, or nothing at all.

    Returns `{calls, input_tokens, candidates}` when Jev answered for every
    sent row and the scores were blended; None -- with every row untouched --
    for any other outcome.
    """
    from ..judge import shapes

    rows = ordered(c for c in pool if c.kind not in ALWAYS)
    sent = rows[:J1_CANDIDATES]
    if judge is None or not sent:
        return None
    state, questions = shapes.j1_rerank(task_type, brief, [j1_row(c) for c in sent])
    try:
        decision = judge.decide("J1", state, questions)
    except Exception:  # noqa: BLE001 -- a refused question is a local-only pack too
        return None
    if not decision.ok or decision.answers is None:
        return None
    p_of = shapes.read_rerank(decision.answers)
    if any(i not in p_of for i in range(len(sent))):
        return None  # §5.5: no partial probabilities are used
    best = max((c.score for c in sent), default=0.0) or 1.0
    for i, cand in enumerate(sent):
        p = min(max(p_of[i], 0.0), 1.0)
        cand.score = round(JEV_WEIGHT * (cand.score / best) + (1 - JEV_WEIGHT) * p, 6)
        cand.extra[JEV_P] = round(p, 4)
        if p < JEV_DEMOTE_BELOW:
            cand.extra[JEV_DEMOTED] = True
    # Rows past the sixty keep their order after the sent ones: Jev was not
    # asked about them, so nothing it said may lift them over a row it read.
    floor = min((c.score for c in sent), default=0.0)
    rest = rows[J1_CANDIDATES:]
    top = max((c.score for c in rest), default=0.0) or 1.0
    for cand in rest:
        cand.score = round(floor * (cand.score / top) * 0.999, 6)
    return {"calls": 1, "input_tokens": decision.input_tokens, "candidates": len(sent)}
