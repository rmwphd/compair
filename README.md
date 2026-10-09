# compair

Self-hosted **pairwise comparison surveys**. Build a list of items, and participants answer "which do you prefer, A or B?" (or *equal* / *skip*) for a fixed number of pairs or as many as they like. A non-PII demographics form comes first. You then see how the ranking changes between demographic groups and subgroups.

- **One small container**: FastAPI + SQLite, comfortably under 300 MB of RAM (numpy, pandas and scipy are most of it). Runs on a Synology DS220+, a $5 VPS or your laptop.
- **Balanced pairing**: never enumerates all n² pairs, so lists of thousands of items are fine. It keeps item exposure even and avoids repeating a pair for the same person.
- **Proper models**: Bradley-Terry with Davidson ties (or ½-win), with bootstrap CIs resampled over participants.
- **Group analysis**: a dashboard with group × group rank tables, bump charts, group-vs-group scatter, Kendall τ, and a likelihood-ratio test for "do groups differ at all?". The notebook adds per-item tests with FDR correction, subgroup clustering, diagnostics and power planning.
- **Privacy**: no names, emails, IPs or user agents are stored. The participant is a random UUID in a cookie.

---

## Quick start (local)

```bash
uv sync                                    # Python 3.11+
cp .env.example .env                       # set ADMIN_PASSWORD
ADMIN_PASSWORD=change-me uv run uvicorn app.main:app --reload
# → http://localhost:8000/admin
```

Fill a demo survey with 300 synthetic participants whose preferences differ by region, age and diet:

```bash
RATE_LIMIT_PER_MIN=100000 ADMIN_PASSWORD=change-me uv run uvicorn app.main:app   # all fake users share your IP
uv run python scripts/simulate.py --base-url http://localhost:8000 --password change-me --participants 300
```

Then open **Results** and group by *Region*: South should rank Jalapeño, Hot honey and Chorizo far higher than everyone else.

## Running a survey

1. **Admin → Create survey.**
2. **Items:** paste one per line (`Label | optional description`) or upload a CSV (`label,description`).
3. **Demographics:** add questions.
   - *Choice*: a dropdown, grouped by its value.
   - *Number*: e.g. age, grouped into the bins you give, such as `18 25 35 50 65` → `<18, 18–24, …, 65+`.
   - *Free text*: exported but never used for grouping.
   - Bins and options can be changed later; stored answers are re-bucketed automatically.
4. **Settings:**
   - prompt text, intro and consent (Markdown);
   - pairs per participant, or *Unlimited* (an "I'm done" button appears);
   - whether ties and skips are allowed.
5. **Preview** while in *draft* (only you, logged in, can see it). Then **Delete all responses** to clear your test answers, and **Open survey**.
6. Share `https://your-host/s/<link-name>`. Participants can use the keyboard: `←` / `→` choose, `↓` equal, `S` skip.
7. **Monitor:** completion, answer coverage per item, position bias, and flagged participants. A participant is flagged when their median answer time is under 400 ms or they pick the same side over 95% of the time.
8. **Results:** group by up to two fields, filter to a subgroup, and toggle whether flagged participants are excluded.
9. **Export ZIP:** tidy CSVs (one row per comparison, with demographic buckets joined) for R, Python or a spreadsheet. **Backup DB** downloads a consistent SQLite snapshot.

### Reading the numbers

| Thing | Meaning |
|---|---|
| **Score** | Bradley-Terry log-strength, centred so the average item is 0. An item at +1 beats an average item ~73% of the time; a gap of 0.5 is ~62/38. |
| **95% interval / rank range** | Bootstrap over participants: how much the score or rank would move with a different sample of people. Overlapping rank ranges mean "can't tell these apart yet". |
| **ν (tie parameter)** | How tie-prone people are (Davidson). 0 means nobody ties. |
| **LRT (likelihood-ratio test)** | "Is one shared ranking enough, or do the groups rank items differently?" A small *p* means they differ. It is approximate for small or sparse groups. |
| **Kendall τ** | Agreement between two groups' orderings: 1 is identical, 0 unrelated. |
| **Min / group** | Groups with fewer participants than this are listed but not ranked. |

Rough guide: aim for at least ~10 answers per item **per group you want to report**. With *n* items and *k* pairs per person, each participant adds about 2k/n answers per item. The notebook's planning cell estimates exactly how many you need for a chosen precision.

## Deep-dive notebook

```bash
uv sync --group notebook
uv run jupyter lab analysis/compair_analysis.ipynb
```

Point it at a copy of the database (`COMPAIR_DB=/path/to/compair.db`, or edit `DB_PATH`) or an export ZIP, and set `SURVEY_SLUG`. It reuses `app/analysis.py`, so its numbers match the dashboard. The notebook is generated from `analysis/build_notebook.py`, so edit that and re-run it.

---

## Deploying on a Synology DS220+

The DS220+ is x86-64 (Celeron J4025), so standard images work.

1. **Package Center:** install **Container Manager**.
2. **File Station:** create `docker/compair/` and inside it `data/`. Copy this repo into `docker/compair/` (or `git clone` over SSH).
3. In `docker/compair/` copy `.env.example` → `.env` and set:
   - `ADMIN_PASSWORD`;
   - `SECRET_KEY` (any long random string);
   - `PUID`/`PGID` to the owner of the `data` folder. SSH in and run `id <your-user>`; the first DSM admin is usually `1026:100`.
4. **Container Manager → Project → Create:**
   - path `/volume1/docker/compair`;
   - source: *Use existing docker-compose.yml*;
   - build and start.
5. Visit `http://<nas-ip>:8000/admin`.

The database lives at `/volume1/docker/compair/data/compair.db` and survives rebuilds and updates. Add that folder to a **Hyper Backup** task. **Backup DB** in the admin also gives you a snapshot that's safe to take while the survey is running.

**Updating:** replace the files (or `git pull`), then in Container Manager open the project and choose **Build** (or run `docker compose up -d --build`).

### Making it reachable from the internet

**Option A: Cloudflare Tunnel (recommended).** Free, HTTPS, and no ports opened on your router. It requires a domain on Cloudflare.

1. Cloudflare dashboard → *Zero Trust → Networks → Tunnels → Create a tunnel* (type *Cloudflared*), then copy the token.
2. Add a *Public hostname*, e.g. `survey.example.com` → service `http://compair:8000`.
3. In `.env` set `TUNNEL_TOKEN=…` and `SECURE_COOKIES=1`.
4. Start with the tunnel profile: `docker compose --profile tunnel up -d`. In Container Manager, add `COMPOSE_PROFILES=tunnel` to `.env` instead.

Consider protecting `/admin*` with a Cloudflare Access policy (email one-time PIN) as a second lock in front of the admin password.

**Option B: Synology reverse proxy.** Use *Control Panel → External Access*:

- *DDNS* gives you `yourname.synology.me`;
- *Security → Certificate* gets a Let's Encrypt certificate;
- *Login Portal → Advanced → Reverse Proxy* maps `https://survey.yourname.synology.me:443` → `http://localhost:8000`.

This needs port 443 forwarded on your router. Set `SECURE_COOKIES=1`.

### Elsewhere

The same image runs anywhere Docker does:

- **VPS** ($4–6/month): `docker compose up -d` with `DATA_PATH=./data`, behind Caddy or the Cloudflare tunnel.
- **AWS EC2 free tier**: step-by-step in [`deploy/aws/README.md`](deploy/aws/README.md). It uses `t3.micro`, Amazon Linux 2023, and the built-in Caddy HTTPS profile (`docker compose --profile https up -d`) on a free `<ip>.sslip.io` hostname.
- **Fly.io**: `fly launch` with a 1 GB volume mounted at `/data`. Keep it to **one machine**, because SQLite is single-writer.

GitHub Pages alone can't work: it serves static files only and cannot store answers.

## Configuration

| Variable | Default | |
|---|---|---|
| `ADMIN_PASSWORD` | *(none)* | Required to log in to `/admin`. |
| `SECRET_KEY` | random per start | Signs the admin session cookie. Set it so logins survive restarts. |
| `SECURE_COOKIES` | `0` | Set to `1` behind HTTPS. |
| `RATE_LIMIT_PER_MIN` | `120` | New participant sessions per minute per IP. Raise it if many people share one network. Answers are separately limited to 90/min per participant. |
| `DATA_DIR` | `./data` (`/data` in Docker) | Where `compair.db` lives. |

## Development

```bash
uv sync --group dev
uv run pytest
```

| Path | What it does |
|---|---|
| `app/pairing.py` | Balanced-random pair selector (pure function `choose_pair` plus a DB wrapper). |
| `app/analysis.py` | Models, bootstrap, group fits, tests and quality flags. Shared with the notebook. |
| `app/surveys.py` | Participant/session data operations. |
| `app/reporting.py` | Dashboard payloads, exports and backup. |
| `app/routes_*.py` | Participant pages, JSON API, admin. |
| `app/static/results.js` | Dashboard charts (plain SVG, no dependencies). |
| `scripts/simulate.py` | Synthetic participants through the real HTTP API. |

`requirements.txt` holds the pinned runtime dependencies for Docker. Regenerate it after changing dependencies:

```bash
uv export --no-default-groups --no-hashes --no-emit-project -o requirements.txt
```
