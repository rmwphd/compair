"""Survey-level data operations shared by participant, API and admin routes."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid

from . import db
from .pairing import next_pair

# --------------------------------------------------------------------------- lookup


def by_slug(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM surveys WHERE slug = ?", (slug,)).fetchone()


def by_id(conn: sqlite3.Connection, survey_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM surveys WHERE id = ?", (survey_id,)).fetchone()


def questions(conn: sqlite3.Connection, survey_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM demo_questions WHERE survey_id = ? ORDER BY sort, id", (survey_id,)
    ).fetchall()


def slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:max_len].strip("-")
    return s or "survey"


def unique_slug(conn: sqlite3.Connection, base: str) -> str:
    slug, n = slugify(base), 2
    while by_slug(conn, slug):
        slug = f"{slugify(base)}-{n}"
        n += 1
    return slug


def question_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:30] or "q"


# --------------------------------------------------------------------------- demographics


def bin_labels(bins: list[float]) -> list[str]:
    """Edges [18, 25, 35] -> ['<18', '18–24', '25–34', '35+'] (integer edges) or half-open ranges."""
    if not bins:
        return []
    ints = all(float(b).is_integer() for b in bins)
    fmt = (lambda v: str(int(v))) if ints else (lambda v: f"{v:g}")
    labels = [f"<{fmt(bins[0])}"]
    for lo, hi in zip(bins, bins[1:]):
        labels.append(f"{fmt(lo)}–{fmt(hi - 1)}" if ints else f"{fmt(lo)}–<{fmt(hi)}")
    labels.append(f"{fmt(bins[-1])}+")
    return labels


def bucket_for(q: sqlite3.Row, value: str | None) -> str | None:
    if value in (None, ""):
        return None
    if q["type"] == "choice":
        return value
    if q["type"] == "number":
        bins = json.loads(q["bins_json"] or "[]")
        if not bins:
            return None
        v = float(value)
        labels = bin_labels(bins)
        for k, edge in enumerate(bins):
            if v < edge:
                return labels[k]
        return labels[-1]
    return None  # free text is never groupable


def validate_answers(qs: list[sqlite3.Row], form: dict) -> tuple[dict[int, str | None], list[str]]:
    values, errors = {}, []
    for q in qs:
        raw = (form.get(f"q_{q['id']}") or "").strip()
        if not raw:
            if q["required"]:
                errors.append(f"Please answer: {q['label']}")
            values[q["id"]] = None
            continue
        if q["type"] == "choice" and raw not in json.loads(q["options_json"] or "[]"):
            errors.append(f"Invalid choice for: {q['label']}")
        elif q["type"] == "number":
            try:
                v = float(raw)
                if not (-1e9 < v < 1e9):
                    raise ValueError
            except ValueError:
                errors.append(f"Please enter a number for: {q['label']}")
        elif q["type"] == "text":
            raw = raw[:500]
        values[q["id"]] = raw
    return values, errors


def save_answers(conn: sqlite3.Connection, participant_id: str, qs: list[sqlite3.Row], values: dict) -> None:
    by_id_ = {q["id"]: q for q in qs}
    with db.tx(conn):
        for qid, value in values.items():
            conn.execute(
                "INSERT OR REPLACE INTO participant_answers (participant_id, question_id, value, bucket) VALUES (?,?,?,?)",
                (participant_id, qid, value, bucket_for(by_id_[qid], value)),
            )
        conn.execute("UPDATE participants SET demo_done = 1 WHERE id = ?", (participant_id,))


def rebucket(conn: sqlite3.Connection, question_id: int) -> None:
    """Recompute stored buckets after a question's bins/options change."""
    q = conn.execute("SELECT * FROM demo_questions WHERE id = ?", (question_id,)).fetchone()
    rows = conn.execute(
        "SELECT participant_id, value FROM participant_answers WHERE question_id = ?", (question_id,)
    ).fetchall()
    with db.tx(conn):
        for r in rows:
            try:
                b = bucket_for(q, r["value"])
            except ValueError:
                b = None
            conn.execute(
                "UPDATE participant_answers SET bucket = ? WHERE participant_id = ? AND question_id = ?",
                (b, r["participant_id"], question_id),
            )


# --------------------------------------------------------------------------- participants


def create_participant(conn: sqlite3.Connection, survey_id: int) -> str:
    pid = str(uuid.uuid4())
    conn.execute("INSERT INTO participants (id, survey_id) VALUES (?, ?)", (pid, survey_id))
    return pid


def participant(conn: sqlite3.Connection, survey_id: int, pid: str | None) -> sqlite3.Row | None:
    if not pid:
        return None
    return conn.execute(
        "SELECT * FROM participants WHERE id = ? AND survey_id = ?", (pid, survey_id)
    ).fetchone()


def progress(conn: sqlite3.Connection, survey: sqlite3.Row, pid: str) -> dict:
    done = conn.execute(
        "SELECT COUNT(*) FROM comparisons WHERE participant_id = ? AND outcome IS NOT NULL", (pid,)
    ).fetchone()[0]
    return {"answered": done, "target": survey["pairs_per_participant"]}


def finish(conn: sqlite3.Connection, pid: str) -> None:
    conn.execute(
        "UPDATE participants SET finished_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE id = ? AND finished_at IS NULL",
        (pid,),
    )
    # Drop the unanswered pair the participant was looking at, if any.
    conn.execute("DELETE FROM comparisons WHERE participant_id = ? AND outcome IS NULL", (pid,))


def _pair_payload(conn, row) -> dict:
    items = {
        r["id"]: {"id": r["id"], "label": r["label"], "description": r["description"]}
        for r in conn.execute(
            "SELECT id, label, description FROM items WHERE id IN (?, ?)", (row["item_left"], row["item_right"])
        )
    }
    return {"comparison_id": row["id"], "left": items[row["item_left"]], "right": items[row["item_right"]]}


def current_or_next(conn: sqlite3.Connection, survey: sqlite3.Row, pid: str) -> dict:
    """Return {"pair": ..., "progress": ...} or {"done": True, ...}.

    The pending (served but unanswered) pair is reused so a page refresh shows
    the same pair instead of minting a new one.
    """
    with db.tx(conn):
        prog = progress(conn, survey, pid)
        pending = conn.execute(
            "SELECT * FROM comparisons WHERE participant_id = ? AND outcome IS NULL ORDER BY seq DESC LIMIT 1",
            (pid,),
        ).fetchone()
        if pending:
            return {"pair": _pair_payload(conn, pending), "progress": prog}
        target = survey["pairs_per_participant"]
        pair = None if (target and prog["answered"] >= target) else next_pair(conn, survey["id"], pid)
        if pair is None:
            finish(conn, pid)
            return {"done": True, "progress": prog}
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM comparisons WHERE participant_id = ?", (pid,)
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO comparisons (participant_id, survey_id, seq, item_left, item_right) VALUES (?,?,?,?,?)",
            (pid, survey["id"], seq, pair[0], pair[1]),
        )
        row = conn.execute("SELECT * FROM comparisons WHERE id = ?", (cur.lastrowid,)).fetchone()
        return {"pair": _pair_payload(conn, row), "progress": prog}


def record_answer(
    conn: sqlite3.Connection, survey: sqlite3.Row, pid: str, comparison_id: int, outcome: str, response_ms: int | None
) -> bool:
    allowed = {"left", "right"} | ({"tie"} if survey["allow_tie"] else set()) | ({"skip"} if survey["allow_skip"] else set())
    if outcome not in allowed:
        raise ValueError(f"outcome must be one of {sorted(allowed)}")
    if response_ms is not None:
        response_ms = max(0, min(int(response_ms), 3_600_000))
    cur = conn.execute(
        """UPDATE comparisons SET outcome = ?, response_ms = ?, answered_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
           WHERE id = ? AND participant_id = ? AND outcome IS NULL""",
        (outcome, response_ms, comparison_id, pid),
    )
    return cur.rowcount == 1
