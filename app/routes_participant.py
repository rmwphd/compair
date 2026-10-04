from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import config, surveys
from .auth import is_admin
from .db import get_conn
from .ratelimit import limit
from .web import templates

router = APIRouter()
COOKIE_MAX_AGE = 60 * 24 * 3600


def cookie_name(survey_id: int) -> str:
    return f"cp_s{survey_id}"


def load_survey(conn, slug: str, request: Request):
    """Return (survey, accessible). Draft surveys are only reachable by a logged-in admin (preview)."""
    s = surveys.by_slug(conn, slug)
    if not s:
        raise HTTPException(404, "Survey not found")
    return s, s["status"] == "open" or (s["status"] == "draft" and is_admin(request))


@router.get("/s/{slug}")
def survey_page(slug: str, request: Request, conn=Depends(get_conn)):
    s, ok = load_survey(conn, slug, request)
    ctx = {"survey": s, "preview": s["status"] == "draft"}
    if not ok:
        return templates.TemplateResponse(request, "closed.html", ctx, status_code=403 if s["status"] == "draft" else 410)
    p = surveys.participant(conn, s["id"], request.cookies.get(cookie_name(s["id"])))
    if not p:
        return templates.TemplateResponse(request, "intro.html", ctx)
    if p["finished_at"]:
        return templates.TemplateResponse(request, "thanks.html", {**ctx, **surveys.progress(conn, s, p["id"])})
    qs = surveys.questions(conn, s["id"])
    if not p["demo_done"] and qs:
        return templates.TemplateResponse(request, "demographics.html", {**ctx, "questions": qs, "errors": [], "values": {}})
    return templates.TemplateResponse(request, "compare.html", ctx)


@router.post("/s/{slug}/start")
async def start(slug: str, request: Request, conn=Depends(get_conn)):
    s, ok = load_survey(conn, slug, request)
    if not ok:
        raise HTTPException(410, "This survey is not accepting responses.")
    limit(request, "start", config.RATE_LIMIT_PER_MIN)
    form = await request.form()
    resp = RedirectResponse(f"/s/{slug}", status_code=303)
    if form.get("website"):  # honeypot: humans never see this field
        return resp
    if not surveys.participant(conn, s["id"], request.cookies.get(cookie_name(s["id"]))):
        pid = surveys.create_participant(conn, s["id"])
        resp.set_cookie(cookie_name(s["id"]), pid, max_age=COOKIE_MAX_AGE, httponly=True,
                        samesite="lax", secure=config.SECURE_COOKIES)
    return resp


@router.post("/s/{slug}/demographics")
async def demographics(slug: str, request: Request, conn=Depends(get_conn)):
    s, ok = load_survey(conn, slug, request)
    if not ok:
        raise HTTPException(410, "This survey is not accepting responses.")
    p = surveys.participant(conn, s["id"], request.cookies.get(cookie_name(s["id"])))
    if not p:
        return RedirectResponse(f"/s/{slug}", status_code=303)
    qs = surveys.questions(conn, s["id"])
    form = dict(await request.form())
    values, errors = surveys.validate_answers(qs, form)
    if errors:
        return templates.TemplateResponse(
            request, "demographics.html",
            {"survey": s, "preview": s["status"] == "draft", "questions": qs, "errors": errors, "values": form},
            status_code=400,
        )
    surveys.save_answers(conn, p["id"], qs, values)
    return RedirectResponse(f"/s/{slug}", status_code=303)
