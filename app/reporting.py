"""Results payloads, quality flags and exports for the admin UI."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
import zipfile
from collections import OrderedDict

import numpy as np
import pandas as pd

from . import analysis as A
from . import db, surveys

# --------------------------------------------------------------------------- quality


def refresh_flags(conn: sqlite3.Connection, survey_id: int) -> pd.DataFrame:
    _, comps, _ = A.load_survey(conn, survey_id)
    q = A.quality_flags(comps)
    flagged = set(q.loc[q["flagged"], "participant_id"]) if not q.empty else set()
    with db.tx(conn):
        conn.execute("UPDATE participants SET flagged = 0 WHERE survey_id = ?", (survey_id,))
        conn.executemany("UPDATE participants SET flagged = 1 WHERE id = ?", [(p,) for p in flagged])
    return q


def _median(values: list) -> int | None:
    return int(np.median(values)) if values else None


def monitor_stats(conn: sqlite3.Connection, survey_id: int) -> dict:
    q = refresh_flags(conn, survey_id)
    one = lambda sql: conn.execute(sql, (survey_id,)).fetchone()[0]  # noqa: E731
    outcomes = dict(conn.execute(
        "SELECT COALESCE(outcome,'pending'), COUNT(*) FROM comparisons WHERE survey_id = ? GROUP BY 1", (survey_id,)
    ).fetchall())
    per_item = conn.execute(
        """SELECT i.label, COUNT(c.id) AS n FROM items i
           LEFT JOIN comparisons c ON c.outcome IN ('left','right','tie') AND (c.item_left = i.id OR c.item_right = i.id)
           WHERE i.survey_id = ? AND i.active = 1 GROUP BY i.id ORDER BY n""",
        (survey_id,),
    ).fetchall()
    counts = [r["n"] for r in per_item]
    answered = sum(outcomes.get(k, 0) for k in ("left", "right", "tie"))
    return {
        "participants": one("SELECT COUNT(*) FROM participants WHERE survey_id = ?"),
        "demo_done": one("SELECT COUNT(*) FROM participants WHERE survey_id = ? AND demo_done = 1"),
        "finished": one("SELECT COUNT(*) FROM participants WHERE survey_id = ? AND finished_at IS NOT NULL"),
        "flagged": one("SELECT COUNT(*) FROM participants WHERE survey_id = ? AND flagged = 1"),
        "outcomes": outcomes,
        "answered": answered,
        "left_share": (outcomes.get("left", 0) / max(outcomes.get("left", 0) + outcomes.get("right", 0), 1)),
        "median_ms": _median([r[0] for r in conn.execute(
            "SELECT response_ms FROM comparisons WHERE survey_id = ? AND response_ms IS NOT NULL", (survey_id,))]),
        "item_min": min(counts) if counts else 0,
        "item_median": int(np.median(counts)) if counts else 0,
        "item_max": max(counts) if counts else 0,
        "least_seen": per_item[:5],
        "flag_table": q[q["flagged"]].head(50).to_dict("records") if not q.empty else [],
    }


# --------------------------------------------------------------------------- results


def groupable_fields(conn: sqlite3.Connection, survey_id: int) -> list[dict]:
    out = []
    for q in surveys.questions(conn, survey_id):
        if q["type"] == "text":
            continue
        if q["type"] == "choice":
            values = json.loads(q["options_json"] or "[]")
        else:
            values = surveys.bin_labels(json.loads(q["bins_json"] or "[]"))
        if values:
            out.append({"key": q["key"], "label": q["label"], "values": values})
    return out


def _items_json(r: A.Result) -> list[dict]:
    cols = ["item_id", "label", "theta", "theta_lo", "theta_hi", "rank", "rank_lo", "rank_hi", "n", "win_pct"]
    t = r.table[[c for c in cols if c in r.table]].replace({np.nan: None})
    recs = t.to_dict("records")
    for rec in recs:
        for k, v in rec.items():
            if isinstance(v, (np.integer,)):
                rec[k] = int(v)
            elif isinstance(v, (np.floating, float)) and v is not None:
                rec[k] = round(float(v), 4)
    return recs


def _result_json(r: A.Result) -> dict:
    return {"n_participants": r.n_participants, "n_comparisons": r.n_comparisons,
            "nu": round(r.fit.nu, 4), "items": _items_json(r)}


_cache: "OrderedDict[tuple, dict]" = OrderedDict()
_cache_lock = threading.Lock()


def results(
    conn: sqlite3.Connection,
    survey_id: int,
    group_by: list[str],
    filters: dict[str, str],
    exclude_flagged: bool = True,
    boot: int = 100,
    ties: str = "davidson",
    min_participants: int = 10,
) -> dict:
    # Cache key includes a data fingerprint so new answers invalidate it.
    fp = conn.execute(
        "SELECT COUNT(*), MAX(answered_at) FROM comparisons WHERE survey_id = ? AND outcome IS NOT NULL", (survey_id,)
    ).fetchone()
    fl = conn.execute("SELECT SUM(flagged) FROM participants WHERE survey_id = ?", (survey_id,)).fetchone()[0]
    key = (survey_id, tuple(group_by), tuple(sorted(filters.items())), exclude_flagged, boot, ties,
           min_participants, tuple(fp), fl)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]

    items, comps, demo = A.load_survey(conn, survey_id, exclude_flagged=exclude_flagged)
    for k, v in filters.items():
        if k in demo:
            demo = demo[demo[k].astype(str) == v]
    comps = comps[comps["participant_id"].isin(demo["participant_id"])]

    payload: dict = {"filters": filters, "group_by": group_by, "exclude_flagged": exclude_flagged,
                     "fields": groupable_fields(conn, survey_id), "min_participants": min_participants}
    if A.answered(comps).empty:
        payload["overall"] = None
    else:
        overall = A.rank_items(comps, items, ties=ties, B=boot, seed=1)
        payload["overall"] = _result_json(overall)
        payload["n_unranked"] = int(len(items) - len(overall.table))

    group_by = [g for g in group_by if g in demo]
    if group_by and payload["overall"]:
        orders = {f["key"]: f["values"] for f in payload["fields"]}
        gr = A.rank_by_group(comps, items, demo, group_by, min_participants=min_participants,
                             orders=orders, ties=ties, B=boot, seed=1)
        corr = A.rank_correlations(gr) if len(gr.groups) > 1 else None
        lrt = A.interaction_lrt(comps, items, demo, group_by, ties=ties, min_participants=min_participants,
                                orders=orders) \
            if len(gr.groups) > 1 else None
        payload["groups"] = {
            "sizes": gr.sizes,
            "insufficient": gr.insufficient,
            "results": {label: _result_json(r) for label, r in gr.groups.items()},
            "tau": None if corr is None else {
                "labels": list(corr.index),
                "matrix": [[None if np.isnan(v) else round(float(v), 3) for v in row] for row in corr.to_numpy()],
            },
            "lrt": lrt,
        }

    with _cache_lock:
        _cache[key] = payload
        while len(_cache) > 32:
            _cache.popitem(last=False)
    return payload


# --------------------------------------------------------------------------- exports


def _csv(df: pd.DataFrame) -> bytes:
    buf = io.StringIO()
    df.to_csv(buf, index=False, quoting=csv.QUOTE_MINIMAL)
    return buf.getvalue().encode("utf-8")


def export_zip(conn: sqlite3.Connection, survey_id: int) -> bytes:
    s = surveys.by_id(conn, survey_id)
    items, comps, demo = A.load_survey(conn, survey_id)
    labels = items.set_index("id")["label"]
    long = comps.rename(columns={"item_left": "item_left_id", "item_right": "item_right_id"})
    long.insert(3, "item_left_label", long["item_left_id"].map(labels))
    long.insert(5, "item_right_label", long["item_right_id"].map(labels))
    long["winner_id"] = np.where(long["outcome"] == "left", long["item_left_id"],
                                 np.where(long["outcome"] == "right", long["item_right_id"], None))
    long = long.merge(demo, on="participant_id", how="left")

    raw_answers = pd.read_sql_query(
        """SELECT pa.participant_id, q.key, pa.value FROM participant_answers pa
           JOIN demo_questions q ON q.id = pa.question_id WHERE q.survey_id = ?""",
        conn, params=(survey_id,))
    qs = pd.read_sql_query("SELECT key, label, type, options_json, bins_json, required FROM demo_questions "
                           "WHERE survey_id = ? ORDER BY sort, id", conn, params=(survey_id,))
    meta = {k: s[k] for k in s.keys()}

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("comparisons.csv", _csv(long))
        z.writestr("items.csv", _csv(items.rename(columns={"id": "item_id"})))
        z.writestr("participants.csv", _csv(demo))
        z.writestr("demographics_raw.csv", _csv(raw_answers))
        z.writestr("questions.csv", _csv(qs))
        z.writestr("survey.json", json.dumps(meta, indent=2))
        z.writestr("README.txt", EXPORT_README)
    return out.getvalue()


EXPORT_README = """compair export
==============
comparisons.csv      one row per pair served. outcome: left|right|tie|skip (empty = served, never answered).
                     Demographic *buckets* for the participant are joined on as extra columns.
items.csv            item_id, label, description, active
participants.csv     participant_id, flagged (auto quality flag), timestamps, one column per demographic bucket
demographics_raw.csv raw demographic answers (e.g. exact age) before bucketing
questions.csv        demographic question definitions
survey.json          survey settings

Load in Python:  from app.analysis import load_export_zip; items, comps, demo = load_export_zip("export.zip")
"""


def backup_bytes(conn: sqlite3.Connection, tmp_path) -> bytes:
    if tmp_path.exists():
        tmp_path.unlink()
    conn.execute("VACUUM INTO ?", (str(tmp_path),))
    data = tmp_path.read_bytes()
    tmp_path.unlink()
    return data
