"""Recall@k math for the retrieval eval — pure, DB-free functions.

The "hit rate @ k" variant pinned by ``evals/retrieval/tests/test_recall.py``
and documented in the baseline TOMLs:

    recall_at_k(ranked_ids, relevant_ids, k) == 1.0
        iff at least one id from ``relevant_ids`` appears in ``ranked_ids[:k]``;
        else 0.0.

This binary-per-query definition fits Fashion200k: most products have exactly
one sibling image, so per-query recall is effectively hit-or-miss. The query
item is never in its own relevant set (the ground-truth builder excludes it and
the runner excludes the query row from the kNN scan), so a ranked list that
contains only the query id scores 0.0 by construction — no special-casing here.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence


def recall_at_k(
    ranked_ids: Sequence[str], relevant_ids: Iterable[str], k: int
) -> float:
    """Return 1.0 if any relevant id is within the top-k of ``ranked_ids``.

    ``ranked_ids`` is the retrieval result, closest-first. ``relevant_ids`` is
    the expected-relevant set for the query (its same-product siblings; never
    the query itself). Anything ranked beyond position ``k`` does not count.
    """
    relevant = set(relevant_ids)
    if not relevant:
        return 0.0
    top_k = ranked_ids[:k]
    return 1.0 if any(rid in relevant for rid in top_k) else 0.0


def mean_recall_at_k(
    per_query: Iterable[tuple[Sequence[str], Iterable[str]]], k: int
) -> float:
    """Average :func:`recall_at_k` over per-query ``(ranked_ids, relevant_ids)``.

    Returns 0.0 for an empty input (no queries → no measured recall) rather than
    raising, so a degenerate fixture set surfaces as a zero metric, not a crash.
    """
    values = [recall_at_k(ranked, relevant, k) for ranked, relevant in per_query]
    if not values:
        return 0.0
    return sum(values) / len(values)
