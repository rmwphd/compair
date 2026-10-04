from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import surveys
from .db import get_conn
from .ratelimit import limit
from .routes_participant import cookie_name, load_survey

router = APIRouter(prefix="/api/s/{slug}")
ANSWERS_PER_MIN = 90  # a human answering this fast is already implausible


class Answer(BaseModel):
    comparison_id: int
    outcome: str
    response_ms: int | None = None


def _session(slug: str, request: Request, conn):
    s, ok = load_survey(conn, slug, request)
    if not ok:
        raise HTTPException(410, "This survey is not accepting responses.")
    p = surveys.participant(conn, s["id"], request.cookies.get(cookie_name(s["id"])))
    if not p:
        raise HTTPException(401, "No session; reload the page.")
    if p["finished_at"]:
        return s, p, {"done": True, "progress": surveys.progress(conn, s, p["id"])}
    if not p["demo_done"] and surveys.questions(conn, s["id"]):
        raise HTTPException(409, "Demographics not completed.")
    return s, p, None


@router.get("/next")
def next_(slug: str, request: Request, conn=Depends(get_conn)):
    s, p, done = _session(slug, request, conn)
    return done or surveys.current_or_next(conn, s, p["id"])


@router.post("/answer")
def answer(slug: str, body: Answer, request: Request, conn=Depends(get_conn)):
    s, p, done = _session(slug, request, conn)
    # Per participant, not per IP: a classroom behind one NAT must not share a budget.
    limit(request, "answer", ANSWERS_PER_MIN, key=p["id"])
    if done:
        return done
    try:
        surveys.record_answer(conn, s, p["id"], body.comparison_id, body.outcome, body.response_ms)
    except ValueError as e:
        raise HTTPException(422, str(e))
    # A stale/duplicate answer (double-tap) is ignored; just return the current state.
    return surveys.current_or_next(conn, s, p["id"])


@router.post("/finish")
def finish(slug: str, request: Request, conn=Depends(get_conn)):
    s, p, done = _session(slug, request, conn)
    if not done:
        surveys.finish(conn, p["id"])
    return {"done": True, "progress": surveys.progress(conn, s, p["id"])}
