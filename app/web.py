"""Shared template environment and small helpers."""

import json
from pathlib import Path

import markdown as md
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
templates.env.filters["markdown"] = lambda s: Markup(md.markdown(s or "", extensions=["extra"]))
templates.env.filters["fromjson"] = lambda s: json.loads(s or "[]")
