import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import config, db


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw")
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MIN", 10_000)
    from app.main import app

    with TestClient(app) as c:
        yield c


def admin(c):
    r = c.post("/admin/login", data={"password": "pw", "next": "/admin"}, follow_redirects=False)
    assert r.status_code == 303


def make_survey(c, n_items=6, pairs=None, questions=True):
    admin(c)
    r = c.post("/admin/surveys", data={"title": "Pizza toppings"}, follow_redirects=False)
    sid = int(r.headers["location"].split("/")[3])
    c.post(f"/admin/surveys/{sid}/items/add", data={"lines": "\n".join(f"Item {i} | desc {i}" for i in range(n_items))})
    settings = {"title": "Pizza toppings", "slug": "pizza", "prompt": "Which is better?", "allow_tie": "on", "allow_skip": "on"}
    if pairs:
        settings["pairs_per_participant"] = str(pairs)
    else:
        settings["unlimited"] = "on"
    c.post(f"/admin/surveys/{sid}/settings", data=settings)
    if questions:
        c.post(f"/admin/surveys/{sid}/demographics/add", data={"label": "Age", "type": "number", "bins": "18 35 60", "required": "on"})
        c.post(f"/admin/surveys/{sid}/demographics/add", data={"label": "Region", "type": "choice", "options": "North\nSouth"})
    c.post(f"/admin/surveys/{sid}/status", data={"status": "open"})
    c.post("/admin/logout")
    c.cookies.clear()
    return sid


def test_admin_requires_login(client):
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/login")
    assert client.post("/admin/login", data={"password": "nope"}).status_code == 401


def test_full_participant_flow_fixed_n(client):
    sid = make_survey(client, pairs=5)
    assert "Start" in client.get("/s/pizza").text
    client.post("/s/pizza/start", data={})
    page = client.get("/s/pizza").text
    assert "Age" in page and "Region" in page
    assert client.post("/s/pizza/demographics", data={}).status_code == 400  # age required
    assert client.post(f"/s/pizza/demographics", data={"q_1": "abc"}).status_code == 400
    r = client.post("/s/pizza/demographics", data={"q_1": "40", "q_2": "South"}, follow_redirects=False)
    assert r.status_code == 303

    state = client.get("/api/s/pizza/next").json()
    # Refresh returns the same pending pair.
    assert client.get("/api/s/pizza/next").json()["pair"]["comparison_id"] == state["pair"]["comparison_id"]
    seen = set()
    for i, outcome in enumerate(["left", "right", "tie", "skip", "left"]):
        pair = state["pair"]
        seen.add(frozenset((pair["left"]["id"], pair["right"]["id"])))
        assert state["progress"]["answered"] == i
        state = client.post("/api/s/pizza/answer", json={"comparison_id": pair["comparison_id"], "outcome": outcome, "response_ms": 1500}).json()
    assert state["done"] and state["progress"]["answered"] == 5
    assert len(seen) == 5
    assert "Thank you" in client.get("/s/pizza").text

    conn = db.connect()
    rows = conn.execute("SELECT outcome FROM comparisons WHERE survey_id = ? ORDER BY seq", (sid,)).fetchall()
    assert [r[0] for r in rows] == ["left", "right", "tie", "skip", "left"]
    b = conn.execute("SELECT bucket FROM participant_answers WHERE question_id = 1").fetchone()[0]
    assert b == "35–59"


def test_unlimited_until_finish_and_exhaustion(client):
    make_survey(client, n_items=4, questions=False)  # 6 possible pairs
    client.post("/s/pizza/start", data={})
    state = client.get("/api/s/pizza/next").json()
    for _ in range(3):
        state = client.post("/api/s/pizza/answer", json={"comparison_id": state["pair"]["comparison_id"], "outcome": "left"}).json()
    assert "pair" in state
    assert client.post("/api/s/pizza/finish").json()["progress"]["answered"] == 3
    # A second participant runs out of pairs after all 6.
    client.cookies.clear()
    client.post("/s/pizza/start", data={})
    state = client.get("/api/s/pizza/next").json()
    n = 0
    while "pair" in state:
        state = client.post("/api/s/pizza/answer", json={"comparison_id": state["pair"]["comparison_id"], "outcome": "right"}).json()
        n += 1
    assert n == 6 and state["done"]


def test_rejects_disallowed_outcome_and_double_answer(client):
    sid = make_survey(client, questions=False, pairs=3)
    admin(client)
    client.post(f"/admin/surveys/{sid}/settings", data={"title": "x", "slug": "pizza", "pairs_per_participant": "3"})  # ties/skips off
    client.post("/s/pizza/start", data={})
    state = client.get("/api/s/pizza/next").json()
    cid = state["pair"]["comparison_id"]
    assert client.post("/api/s/pizza/answer", json={"comparison_id": cid, "outcome": "tie"}).status_code == 422
    nxt = client.post("/api/s/pizza/answer", json={"comparison_id": cid, "outcome": "left"}).json()
    again = client.post("/api/s/pizza/answer", json={"comparison_id": cid, "outcome": "right"}).json()
    assert again["pair"]["comparison_id"] == nxt["pair"]["comparison_id"]  # double-tap ignored
    assert again["progress"]["answered"] == 1


def test_closed_and_draft_surveys(client):
    sid = make_survey(client)
    admin(client)
    client.post(f"/admin/surveys/{sid}/status", data={"status": "closed"})
    client.post("/admin/logout")
    client.cookies.clear()
    assert client.get("/s/pizza").status_code == 410
    assert client.post("/s/pizza/start").status_code == 410


def test_honeypot_creates_no_participant(client):
    make_survey(client)
    client.post("/s/pizza/start", data={"website": "spam"})
    assert db.connect().execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0


def test_admin_pages_results_and_export(client):
    sid = make_survey(client, n_items=8, pairs=10)
    for k in range(12):
        client.cookies.clear()
        client.post("/s/pizza/start", data={})
        client.post("/s/pizza/demographics", data={"q_1": str(20 + 5 * k), "q_2": ["North", "South"][k % 2]})
        state = client.get("/api/s/pizza/next").json()
        while "pair" in state:
            p = state["pair"]
            # Lower-numbered items win for North, higher for South.
            lo_wins = (p["left"]["id"] < p["right"]["id"]) == (k % 2 == 0)
            state = client.post("/api/s/pizza/answer", json={"comparison_id": p["comparison_id"], "outcome": "left" if lo_wins else "right", "response_ms": 1200}).json()
    client.cookies.clear()
    admin(client)
    for path in ["", "/items", "/demographics", "/monitor", "/results"]:
        r = client.get(f"/admin/surveys/{sid}{path}")
        assert r.status_code == 200, path
    assert client.get("/admin").status_code == 200

    res = client.get(f"/admin/api/surveys/{sid}/results?group=region&boot=20&min=3").json()
    assert res["overall"]["n_participants"] == 12
    g = res["groups"]
    assert set(g["results"]) == {"North", "South"}
    assert g["tau"]["matrix"][0][1] < -0.5  # opposite preferences
    assert g["lrt"]["p_value"] < 0.01
    filt = client.get(f"/admin/api/surveys/{sid}/results?filter=region:North&boot=0").json()
    assert filt["overall"]["n_participants"] == 6

    z = zipfile.ZipFile(io.BytesIO(client.get(f"/admin/surveys/{sid}/export.zip").content))
    assert {"comparisons.csv", "items.csv", "participants.csv", "survey.json"} <= set(z.namelist())
    from app.analysis import load_export_zip
    items, comps, demo = load_export_zip(client.get(f"/admin/surveys/{sid}/export.zip").content)
    assert len(items) == 8 and len(demo) == 12 and "region" in demo
    assert client.get("/admin/backup.db").content[:15] == b"SQLite format 3"

    # Re-binning a number question re-buckets stored answers.
    client.post(f"/admin/surveys/{sid}/demographics/1/edit", data={"label": "Age", "bins": "50", "required": "on"})
    buckets = {r[0] for r in db.connect().execute("SELECT bucket FROM participant_answers WHERE question_id = 1")}
    assert buckets == {"<50", "50+"}
