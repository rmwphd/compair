import csv
import io
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response

from . import auth, config, db, reporting, surveys
from .db import get_conn
from .ratelimit import limit
from .web import templates

router = APIRouter(prefix="/admin")
guard = [Depends(auth.require_admin)]

TABS = [("settings", "Settings"), ("items", "Items"), ("demographics", "Demographics"),
        ("monitor", "Monitor"), ("results", "Results")]


def back(path: str, msg: str | None = None) -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse(path + (f"?msg={quote(msg)}" if msg else ""), status_code=303)


def get_survey_or_404(conn, survey_id: int):
    s = surveys.by_id(conn, survey_id)
    if not s:
        raise HTTPException(404, "Survey not found")
    return s


def page(request, conn, survey, tab, **ctx):
    return templates.TemplateResponse(request, f"admin/{tab}.html", {
        "survey": survey, "tab": tab, "tabs": TABS, "msg": request.query_params.get("msg"),
        "public_url": str(request.base_url).rstrip("/") + f"/s/{survey['slug']}", **ctx})


# --------------------------------------------------------------------------- auth


@router.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "admin/login.html", {
        "configured": bool(config.ADMIN_PASSWORD), "next": request.query_params.get("next", "/admin"), "error": None})


@router.post("/login")
async def login(request: Request):
    limit(request, "login", 10)
    form = await request.form()
    nxt = str(form.get("next") or "/admin")
    if not nxt.startswith("/admin"):
        nxt = "/admin"
    if not auth.check_password(str(form.get("password", ""))):
        return templates.TemplateResponse(request, "admin/login.html", {
            "configured": bool(config.ADMIN_PASSWORD), "next": nxt, "error": "Wrong password."}, status_code=401)
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(auth.COOKIE, auth.session_token(), max_age=auth.MAX_AGE, httponly=True,
                    samesite="lax", secure=config.SECURE_COOKIES)
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


# --------------------------------------------------------------------------- surveys


@router.get("", dependencies=guard)
def index(request: Request, conn=Depends(get_conn)):
    rows = conn.execute("""
        SELECT s.*,
          (SELECT COUNT(*) FROM items i WHERE i.survey_id = s.id AND i.active = 1) AS n_items,
          (SELECT COUNT(*) FROM participants p WHERE p.survey_id = s.id) AS n_participants,
          (SELECT COUNT(*) FROM comparisons c WHERE c.survey_id = s.id AND c.outcome IN ('left','right','tie')) AS n_answers
        FROM surveys s ORDER BY s.created_at DESC""").fetchall()
    return templates.TemplateResponse(request, "admin/index.html", {
        "surveys": rows, "msg": request.query_params.get("msg")})


@router.post("/surveys", dependencies=guard)
async def create_survey(request: Request, conn=Depends(get_conn)):
    form = await request.form()
    title = str(form.get("title", "")).strip() or "Untitled survey"
    cur = conn.execute("INSERT INTO surveys (slug, title, pairs_per_participant) VALUES (?, ?, 20)",
                       (surveys.unique_slug(conn, title), title))
    return back(f"/admin/surveys/{cur.lastrowid}/items", "Survey created. Add some items next.")


@router.get("/surveys/{survey_id}", dependencies=guard)
def survey_settings(survey_id: int, request: Request, conn=Depends(get_conn)):
    s = get_survey_or_404(conn, survey_id)
    n_part = conn.execute("SELECT COUNT(*) FROM participants WHERE survey_id = ?", (survey_id,)).fetchone()[0]
    return page(request, conn, s, "settings", n_participants=n_part)


@router.post("/surveys/{survey_id}/settings", dependencies=guard)
async def save_settings(survey_id: int, request: Request, conn=Depends(get_conn)):
    s = get_survey_or_404(conn, survey_id)
    f = await request.form()
    slug = surveys.slugify(str(f.get("slug") or s["slug"]))
    other = surveys.by_slug(conn, slug)
    if other and other["id"] != survey_id:
        return back(f"/admin/surveys/{survey_id}", f"The link name “{slug}” is taken by another survey.")
    unlimited = f.get("unlimited") == "on"
    try:
        n_pairs = None if unlimited else max(1, int(f.get("pairs_per_participant") or 20))
    except ValueError:
        return back(f"/admin/surveys/{survey_id}", "Pairs per participant must be a whole number.")
    conn.execute(
        """UPDATE surveys SET title=?, slug=?, prompt=?, intro_md=?, consent_md=?, pairs_per_participant=?,
           allow_tie=?, allow_skip=? WHERE id=?""",
        (str(f.get("title") or s["title"]).strip(), slug, str(f.get("prompt") or "Which do you prefer?").strip(),
         str(f.get("intro_md") or ""), str(f.get("consent_md") or ""), n_pairs,
         int(f.get("allow_tie") == "on"), int(f.get("allow_skip") == "on"), survey_id),
    )
    return back(f"/admin/surveys/{survey_id}", "Saved.")


@router.post("/surveys/{survey_id}/status", dependencies=guard)
async def set_status(survey_id: int, request: Request, conn=Depends(get_conn)):
    get_survey_or_404(conn, survey_id)
    status = (await request.form()).get("status")
    if status not in ("draft", "open", "closed"):
        raise HTTPException(400, "bad status")
    if status == "open":
        n = conn.execute("SELECT COUNT(*) FROM items WHERE survey_id = ? AND active = 1", (survey_id,)).fetchone()[0]
        if n < 2:
            return back(f"/admin/surveys/{survey_id}", "Add at least two active items before opening.")
    conn.execute("UPDATE surveys SET status = ? WHERE id = ?", (status, survey_id))
    return back(f"/admin/surveys/{survey_id}", f"Survey is now {status}.")


@router.post("/surveys/{survey_id}/duplicate", dependencies=guard)
def duplicate(survey_id: int, conn=Depends(get_conn)):
    s = get_survey_or_404(conn, survey_id)
    with db.tx(conn):
        cur = conn.execute(
            """INSERT INTO surveys (slug, title, prompt, intro_md, consent_md, pairs_per_participant, allow_tie, allow_skip)
               VALUES (?,?,?,?,?,?,?,?)""",
            (surveys.unique_slug(conn, s["slug"]), s["title"] + " (copy)", s["prompt"], s["intro_md"],
             s["consent_md"], s["pairs_per_participant"], s["allow_tie"], s["allow_skip"]))
        new = cur.lastrowid
        conn.execute("INSERT INTO items (survey_id, label, description, active) "
                     "SELECT ?, label, description, active FROM items WHERE survey_id = ? ORDER BY id", (new, survey_id))
        conn.execute("""INSERT INTO demo_questions (survey_id, key, label, type, options_json, bins_json, required, sort)
                        SELECT ?, key, label, type, options_json, bins_json, required, sort
                        FROM demo_questions WHERE survey_id = ?""", (new, survey_id))
    return back(f"/admin/surveys/{new}", "Duplicated (items and questions copied, no responses).")


@router.post("/surveys/{survey_id}/reset", dependencies=guard)
async def reset_responses(survey_id: int, request: Request, conn=Depends(get_conn)):
    s = get_survey_or_404(conn, survey_id)
    if (await request.form()).get("confirm") != s["slug"]:
        return back(f"/admin/surveys/{survey_id}", "Type the link name exactly to confirm.")
    conn.execute("DELETE FROM participants WHERE survey_id = ?", (survey_id,))  # cascades
    return back(f"/admin/surveys/{survey_id}", "All responses deleted.")


@router.post("/surveys/{survey_id}/delete", dependencies=guard)
async def delete_survey(survey_id: int, request: Request, conn=Depends(get_conn)):
    s = get_survey_or_404(conn, survey_id)
    if (await request.form()).get("confirm") != s["slug"]:
        return back(f"/admin/surveys/{survey_id}", "Type the link name exactly to confirm.")
    conn.execute("DELETE FROM surveys WHERE id = ?", (survey_id,))
    return back("/admin", f"Deleted “{s['title']}”.")


# --------------------------------------------------------------------------- items


@router.get("/surveys/{survey_id}/items", dependencies=guard)
def items_page(survey_id: int, request: Request, conn=Depends(get_conn)):
    s = get_survey_or_404(conn, survey_id)
    q = request.query_params.get("q", "").strip()
    rows = conn.execute(
        """SELECT i.*, (SELECT COUNT(*) FROM comparisons c WHERE c.outcome IN ('left','right','tie')
                        AND (c.item_left = i.id OR c.item_right = i.id)) AS n
           FROM items i WHERE i.survey_id = ? AND (? = '' OR i.label LIKE ?) ORDER BY i.id""",
        (survey_id, q, f"%{q}%")).fetchall()
    counts = conn.execute("SELECT COUNT(*), SUM(active) FROM items WHERE survey_id = ?", (survey_id,)).fetchone()
    return page(request, conn, s, "items", items=rows, q=q, n_total=counts[0], n_active=counts[1] or 0)


def _insert_items(conn, survey_id, pairs):
    existing = {r[0].casefold() for r in conn.execute("SELECT label FROM items WHERE survey_id = ?", (survey_id,))}
    added = 0
    with db.tx(conn):
        for label, desc in pairs:
            label, desc = label.strip()[:300], desc.strip()[:2000]
            if not label or label.casefold() in existing:
                continue
            existing.add(label.casefold())
            conn.execute("INSERT INTO items (survey_id, label, description) VALUES (?,?,?)", (survey_id, label, desc))
            added += 1
    return added


@router.post("/surveys/{survey_id}/items/add", dependencies=guard)
async def add_items(survey_id: int, request: Request, conn=Depends(get_conn)):
    get_survey_or_404(conn, survey_id)
    text = str((await request.form()).get("lines", ""))
    pairs = []
    for line in text.splitlines():
        label, _, desc = line.partition("|")
        pairs.append((label, desc))
    n = _insert_items(conn, survey_id, pairs)
    return back(f"/admin/surveys/{survey_id}/items", f"Added {n} item(s); duplicates and blanks skipped.")


@router.post("/surveys/{survey_id}/items/upload", dependencies=guard)
async def upload_items(survey_id: int, file: UploadFile, conn=Depends(get_conn)):
    get_survey_or_404(conn, survey_id)
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(raw)))
    if rows and rows[0] and rows[0][0].strip().lower() in ("label", "item", "name"):
        rows = rows[1:]
    n = _insert_items(conn, survey_id, [(r[0], r[1] if len(r) > 1 else "") for r in rows if r])
    return back(f"/admin/surveys/{survey_id}/items", f"Imported {n} item(s) from {file.filename}.")


@router.post("/surveys/{survey_id}/items/{item_id}/toggle", dependencies=guard)
def toggle_item(survey_id: int, item_id: int, conn=Depends(get_conn)):
    conn.execute("UPDATE items SET active = 1 - active WHERE id = ? AND survey_id = ?", (item_id, survey_id))
    return back(f"/admin/surveys/{survey_id}/items")


@router.post("/surveys/{survey_id}/items/{item_id}/edit", dependencies=guard)
async def edit_item(survey_id: int, item_id: int, request: Request, conn=Depends(get_conn)):
    f = await request.form()
    label = str(f.get("label", "")).strip()[:300]
    if label:
        conn.execute("UPDATE items SET label = ?, description = ? WHERE id = ? AND survey_id = ?",
                     (label, str(f.get("description", "")).strip()[:2000], item_id, survey_id))
    return back(f"/admin/surveys/{survey_id}/items")


@router.post("/surveys/{survey_id}/items/{item_id}/delete", dependencies=guard)
def delete_item(survey_id: int, item_id: int, conn=Depends(get_conn)):
    used = conn.execute("SELECT 1 FROM comparisons WHERE item_left = ? OR item_right = ? LIMIT 1",
                        (item_id, item_id)).fetchone()
    if used:
        return back(f"/admin/surveys/{survey_id}/items", "That item has responses; deactivate it instead.")
    conn.execute("DELETE FROM items WHERE id = ? AND survey_id = ?", (item_id, survey_id))
    return back(f"/admin/surveys/{survey_id}/items", "Item deleted.")


# --------------------------------------------------------------------------- demographics


def _parse_question(f) -> tuple[dict, str | None]:
    qtype = str(f.get("type", "choice"))
    if qtype not in ("choice", "number", "text"):
        return {}, "Unknown question type."
    label = str(f.get("label", "")).strip()
    if not label:
        return {}, "Question text is required."
    options = [o.strip() for o in str(f.get("options", "")).splitlines() if o.strip()]
    try:
        bins = sorted({float(b) for b in str(f.get("bins", "")).replace(",", " ").split()})
    except ValueError:
        return {}, "Bins must be numbers, e.g. 18 25 35 50 65."
    if qtype == "choice" and len(options) < 2:
        return {}, "A choice question needs at least two options (one per line)."
    return {"label": label, "type": qtype, "options_json": json.dumps(options if qtype == "choice" else []),
            "bins_json": json.dumps([int(b) if b.is_integer() else b for b in bins] if qtype == "number" else []),
            "required": int(f.get("required") == "on")}, None


@router.get("/surveys/{survey_id}/demographics", dependencies=guard)
def demographics_page(survey_id: int, request: Request, conn=Depends(get_conn)):
    s = get_survey_or_404(conn, survey_id)
    qs = surveys.questions(conn, survey_id)
    answered = {r[0]: r[1] for r in conn.execute(
        """SELECT question_id, COUNT(*) FROM participant_answers pa JOIN demo_questions q ON q.id = pa.question_id
           WHERE q.survey_id = ? AND pa.value IS NOT NULL GROUP BY question_id""", (survey_id,))}
    bins = {q["id"]: surveys.bin_labels(json.loads(q["bins_json"])) for q in qs}
    return page(request, conn, s, "demographics", questions=qs, answered=answered, bin_labels=bins)


@router.post("/surveys/{survey_id}/demographics/add", dependencies=guard)
async def add_question(survey_id: int, request: Request, conn=Depends(get_conn)):
    get_survey_or_404(conn, survey_id)
    q, err = _parse_question(await request.form())
    if err:
        return back(f"/admin/surveys/{survey_id}/demographics", err)
    key, base, n = surveys.question_key(q["label"]), surveys.question_key(q["label"]), 2
    while conn.execute("SELECT 1 FROM demo_questions WHERE survey_id = ? AND key = ?", (survey_id, key)).fetchone():
        key, n = f"{base}_{n}", n + 1
    sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM demo_questions WHERE survey_id = ?",
                        (survey_id,)).fetchone()[0]
    conn.execute("""INSERT INTO demo_questions (survey_id, key, label, type, options_json, bins_json, required, sort)
                    VALUES (?,?,?,?,?,?,?,?)""",
                 (survey_id, key, q["label"], q["type"], q["options_json"], q["bins_json"], q["required"], sort))
    return back(f"/admin/surveys/{survey_id}/demographics", "Question added.")


@router.post("/surveys/{survey_id}/demographics/{qid}/edit", dependencies=guard)
async def edit_question(survey_id: int, qid: int, request: Request, conn=Depends(get_conn)):
    f = await request.form()
    old = conn.execute("SELECT * FROM demo_questions WHERE id = ? AND survey_id = ?", (qid, survey_id)).fetchone()
    if not old:
        raise HTTPException(404)
    q, err = _parse_question({**dict(f), "type": old["type"]})
    if err:
        return back(f"/admin/surveys/{survey_id}/demographics", err)
    conn.execute("UPDATE demo_questions SET label=?, options_json=?, bins_json=?, required=? WHERE id=?",
                 (q["label"], q["options_json"], q["bins_json"], q["required"], qid))
    surveys.rebucket(conn, qid)
    return back(f"/admin/surveys/{survey_id}/demographics", "Question updated; existing answers re-bucketed.")


@router.post("/surveys/{survey_id}/demographics/{qid}/move", dependencies=guard)
async def move_question(survey_id: int, qid: int, request: Request, conn=Depends(get_conn)):
    direction = -1 if (await request.form()).get("dir") == "up" else 1
    ids = [q["id"] for q in surveys.questions(conn, survey_id)]
    if qid in ids:
        k = ids.index(qid)
        j = k + direction
        if 0 <= j < len(ids):
            ids[k], ids[j] = ids[j], ids[k]
            with db.tx(conn):
                for sort, i in enumerate(ids):
                    conn.execute("UPDATE demo_questions SET sort = ? WHERE id = ?", (sort, i))
    return back(f"/admin/surveys/{survey_id}/demographics")


@router.post("/surveys/{survey_id}/demographics/{qid}/delete", dependencies=guard)
def delete_question(survey_id: int, qid: int, conn=Depends(get_conn)):
    conn.execute("DELETE FROM demo_questions WHERE id = ? AND survey_id = ?", (qid, survey_id))
    return back(f"/admin/surveys/{survey_id}/demographics", "Question deleted (with its answers).")


# --------------------------------------------------------------------------- monitor / results / export


@router.get("/surveys/{survey_id}/monitor", dependencies=guard)
def monitor(survey_id: int, request: Request, conn=Depends(get_conn)):
    s = get_survey_or_404(conn, survey_id)
    return page(request, conn, s, "monitor", stats=reporting.monitor_stats(conn, survey_id))


@router.get("/surveys/{survey_id}/results", dependencies=guard)
def results_page(survey_id: int, request: Request, conn=Depends(get_conn)):
    s = get_survey_or_404(conn, survey_id)
    return page(request, conn, s, "results", fields=reporting.groupable_fields(conn, survey_id))


@router.get("/api/surveys/{survey_id}/results", dependencies=guard)
def results_json(survey_id: int, request: Request, conn=Depends(get_conn)):
    get_survey_or_404(conn, survey_id)
    qp = request.query_params
    group_by = [g for g in qp.getlist("group") if g][:2]
    filters = {}
    for f in qp.getlist("filter"):
        k, _, v = f.partition(":")
        if k and v:
            filters[k] = v
    reporting.refresh_flags(conn, survey_id)
    payload = reporting.results(
        conn, survey_id, group_by, filters,
        exclude_flagged=qp.get("exclude_flagged", "1") == "1",
        boot=max(0, min(int(qp.get("boot", 100)), 500)),
        ties="half" if qp.get("ties") == "half" else "davidson",
        min_participants=max(1, int(qp.get("min", 10))),
    )
    return JSONResponse(payload)


@router.get("/surveys/{survey_id}/export.zip", dependencies=guard)
def export(survey_id: int, conn=Depends(get_conn)):
    s = get_survey_or_404(conn, survey_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return Response(reporting.export_zip(conn, survey_id), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{s["slug"]}-{stamp}.zip"'})


@router.get("/backup.db", dependencies=guard)
def backup(conn=Depends(get_conn)):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    data = reporting.backup_bytes(conn, config.DATA_DIR / ".backup-tmp.db")
    return Response(data, media_type="application/x-sqlite3",
                    headers={"Content-Disposition": f'attachment; filename="compair-{stamp}.db"'})


@router.get("/api/survey-by-slug/{slug}", dependencies=guard)
def survey_by_slug(slug: str, conn=Depends(get_conn)):
    s = surveys.by_slug(conn, slug)
    if not s:
        raise HTTPException(404, "Survey not found")
    qs = [{"id": q["id"], "key": q["key"], "label": q["label"], "type": q["type"]}
          for q in surveys.questions(conn, s["id"])]
    return {"id": s["id"], "slug": s["slug"], "status": s["status"], "questions": qs}
