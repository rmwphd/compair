"""Balanced-random pair selection.

Never materialises all n^2 pairs. Each call:
  1. ranks active items by how often they've been served (plus random jitter),
  2. takes item A from the least-exposed pool,
  3. picks B among the other items, preferring low item exposure and low
     global count for the specific (A, B) pair, and excluding pairs this
     participant has already seen.
Sides are randomised to cancel position bias.
"""

from __future__ import annotations

import random
import sqlite3


def _item_exposure(conn: sqlite3.Connection, survey_id: int) -> dict[int, int]:
    rows = conn.execute(
        """
        SELECT item, COUNT(*) AS n FROM (
            SELECT item_left AS item FROM comparisons WHERE survey_id = ?
            UNION ALL
            SELECT item_right FROM comparisons WHERE survey_id = ?
        ) GROUP BY item
        """,
        (survey_id, survey_id),
    ).fetchall()
    return {r["item"]: r["n"] for r in rows}


def _pair_counts_for(conn: sqlite3.Connection, survey_id: int, item: int) -> dict[int, int]:
    rows = conn.execute(
        """
        SELECT CASE WHEN item_left = ? THEN item_right ELSE item_left END AS other, COUNT(*) AS n
        FROM comparisons
        WHERE survey_id = ? AND (item_left = ? OR item_right = ?)
        GROUP BY other
        """,
        (item, survey_id, item, item),
    ).fetchall()
    return {r["other"]: r["n"] for r in rows}


def _seen_pairs(conn: sqlite3.Connection, participant_id: str) -> set[tuple[int, int]]:
    rows = conn.execute(
        "SELECT item_left, item_right FROM comparisons WHERE participant_id = ?",
        (participant_id,),
    ).fetchall()
    return {(min(a, b), max(a, b)) for a, b in rows}


def choose_pair(
    item_ids: list[int],
    exposure: dict[int, int],
    seen: set[tuple[int, int]],
    pair_counts_for,
    rng: random.Random,
    pool_frac: float = 0.25,
) -> tuple[int, int] | None:
    """Pure selection logic (DB-free so it can be unit-tested and simulated)."""
    n = len(item_ids)
    if n < 2 or len(seen) >= n * (n - 1) // 2:
        return None

    # Order by exposure with random tie-breaking.
    ordered = sorted(item_ids, key=lambda i: (exposure.get(i, 0), rng.random()))
    pool = max(2, int(n * pool_frac + 0.5))
    candidates_a = ordered[:pool]
    rng.shuffle(candidates_a)
    # Fall back to the rest (still least-exposed first) if the pool is exhausted for this participant.
    candidates_a += ordered[pool:]

    seen_count: dict[int, int] = {}
    for a, b in seen:
        seen_count[a] = seen_count.get(a, 0) + 1
        seen_count[b] = seen_count.get(b, 0) + 1

    for a in candidates_a:
        if seen_count.get(a, 0) >= n - 1:
            continue  # participant has already compared A with everything
        pc = pair_counts_for(a)
        best, best_score = None, None
        for b in item_ids:
            if b == a or (min(a, b), max(a, b)) in seen:
                continue
            # Pair repetition is penalised harder than item exposure: coverage of
            # distinct pairs is what makes the comparison graph informative.
            score = exposure.get(b, 0) + 3 * pc.get(b, 0) + rng.random()
            if best_score is None or score < best_score:
                best, best_score = b, score
        if best is not None:
            return (a, best) if rng.random() < 0.5 else (best, a)
    return None


def next_pair(
    conn: sqlite3.Connection, survey_id: int, participant_id: str, rng: random.Random | None = None
) -> tuple[int, int] | None:
    rng = rng or random.Random()
    item_ids = [
        r["id"]
        for r in conn.execute("SELECT id FROM items WHERE survey_id = ? AND active = 1", (survey_id,))
    ]
    return choose_pair(
        item_ids,
        _item_exposure(conn, survey_id),
        _seen_pairs(conn, participant_id),
        lambda a: _pair_counts_for(conn, survey_id, a),
        rng,
    )
