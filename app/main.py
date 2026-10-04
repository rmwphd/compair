from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db, routes_admin, routes_api, routes_participant


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    yield


app = FastAPI(title="CompAIR", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")
app.include_router(routes_participant.router)
app.include_router(routes_api.router)
app.include_router(routes_admin.router)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/admin")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}
