"""Generates compair_analysis.ipynb (kept as code so the notebook diff stays reviewable).

    uv run python analysis/build_notebook.py
"""

import json
from pathlib import Path

cells = []


def md(s):
    cells.append({"cell_type": "markdown", "metadata": {}, "source": s.strip("\n")})


def code(s):
    cells.append({"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [], "source": s.strip("\n")})


md(r"""
# compair: deep-dive analysis

Companion to the admin dashboard. It uses the same model code (`app/analysis.py`) and adds tests and diagnostics that don't fit a dashboard:

1. **Data & overall ranking**: Davidson vs ½-win tie handling, bootstrap CIs
2. **Which demographics matter?**: likelihood-ratio test per field
3. **Two-group contrast**: per-item Wald tests with FDR correction
4. **Subgroup structure**: rank-correlation matrix + clustering of subgroups
5. **Diagnostics**: position bias, response times, flagged-participant sensitivity
6. **Planning**: power and ranking accuracy for any number of participants × pairs each

Set the source below: either the live SQLite file (copy it from the NAS or use *Backup DB* in the admin) or an *Export ZIP*.
""")

code(r"""
import os, sys, sqlite3
from pathlib import Path

ROOT = Path.cwd() if (Path.cwd() / "app").exists() else Path.cwd().parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from scipy import stats
from scipy.cluster import hierarchy
from app import analysis as A

# ---- configure -------------------------------------------------------------
DB_PATH = ROOT / os.environ.get("COMPAIR_DB", "data/compair.db")  # relative to the repo root; or set EXPORT_ZIP
EXPORT_ZIP = None                         # e.g. ROOT / "demo-20261004-1200.zip"
SURVEY_SLUG = os.environ.get("COMPAIR_SLUG", "demo")
EXCLUDE_FLAGGED = True
MIN_PARTICIPANTS = 10                     # per group
BOOT = 200
# -----------------------------------------------------------------------------

ORDERS = {}  # value order per field (age bands low->high, etc.)
if EXPORT_ZIP:
    items, comps, demo = A.load_export_zip(EXPORT_ZIP)
    if EXCLUDE_FLAGGED:
        keep = set(demo.loc[demo.flagged == 0, "participant_id"])
        comps, demo = comps[comps.participant_id.isin(keep)], demo[demo.participant_id.isin(keep)]
else:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)  # read-only: never touches the backup
    conn.row_factory = sqlite3.Row
    sid = conn.execute("SELECT id FROM surveys WHERE slug = ?", (SURVEY_SLUG,)).fetchone()[0]
    items, comps, demo = A.load_survey(conn, sid, exclude_flagged=EXCLUDE_FLAGGED)
    from app.reporting import groupable_fields
    ORDERS = {f["key"]: f["values"] for f in groupable_fields(conn, sid)}

FIELDS = [c for c in demo.columns if c not in ("participant_id", "flagged", "started_at", "finished_at")
          and demo[c].nunique(dropna=True) >= 2 and demo[c].nunique() <= 30]
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]  # dataviz reference palette, slots 1-3
plt.rcParams.update({"figure.dpi": 110, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.grid": True, "grid.color": "#e1e0d9", "axes.axisbelow": True})

ans = A.answered(comps)
print(f"{len(items)} items · {demo.participant_id.nunique()} participants · {len(ans)} answered comparisons")
print("outcomes:", comps.outcome.fillna("pending").value_counts().to_dict())
print("groupable fields:", FIELDS)
""")

md("## 1. Overall ranking\nDavidson treats ties as their own outcome (estimating ν); the ½-win model splits them. If the two orderings agree closely, tie handling doesn't matter much for your conclusions.")

code(r"""
dav = A.rank_items(comps, items, ties="davidson", B=BOOT, seed=1)
half = A.rank_items(comps, items, ties="half", B=0)
both = dav.table.merge(half.table[["item_id", "theta", "rank"]], on="item_id", suffixes=("", "_half"))
rho = stats.spearmanr(both.theta, both.theta_half).statistic
print(f"tie parameter ν = {dav.fit.nu:.3f}   ·   Spearman(Davidson, ½-win) = {rho:.3f}")
both[["rank", "label", "theta", "theta_lo", "theta_hi", "rank_lo", "rank_hi", "win_pct", "n", "rank_half"]].round(3)
""")

code(r"""
t = dav.table.iloc[::-1]
fig, ax = plt.subplots(figsize=(7, 0.28 * len(t) + 1))
ax.hlines(range(len(t)), t.theta_lo, t.theta_hi, color=SERIES[0], alpha=.5, lw=2)
ax.plot(t.theta, range(len(t)), "o", color=SERIES[0], mec="white", mew=1.5, ms=7)
ax.axvline(0, color="#c3c2b7", lw=1)
ax.set_yticks(range(len(t)), t.label)
ax.set_xlabel("Bradley-Terry score (0 = average item), 95% bootstrap CI")
ax.set_title("Overall ranking", loc="left")
plt.tight_layout()
""")

md("## 2. Which demographics matter?\nFor each field: one shared ranking vs one ranking per group (likelihood-ratio test). Small *p* ⇒ that field's groups rank items differently. Fields are tested one at a time; with several fields, read the *p*-values with a multiple-testing eye (Holm column).")

code(r"""
rows = []
for f in FIELDS:
    r = A.interaction_lrt(comps, items, demo, [f], min_participants=MIN_PARTICIPANTS, orders=ORDERS)
    if r:
        rows.append({"field": f, "groups": len(r["groups"]), "chi2": r["statistic"], "df": r["df"], "p": r["p_value"],
                     "chi2/df": r["statistic"] / r["df"]})
lrt = pd.DataFrame(rows).sort_values("p")
if len(lrt):
    from statsmodels.stats.multitest import multipletests
    lrt["p_holm"] = multipletests(lrt.p, method="holm")[1]
lrt.round(4)
""")

md("## 3. Two-group contrast\nPick a field and two of its groups. Each item's score difference gets a Wald test; *q* is the Benjamini-Hochberg FDR-adjusted *p*. Scores are centred within each group, so a difference means \"relative to that group's average item\".")

code(r"""
FIELD = lrt.field.iloc[0] if len(lrt) else FIELDS[0]   # most significant field by default
sizes = demo[FIELD].value_counts()
G1, G2 = sizes.index[:2]                                  # two largest groups; override as needed
print(f"{FIELD}: {G1} (n={sizes[G1]}) vs {G2} (n={sizes[G2]})")

def subset(g):
    ids = set(demo.loc[demo[FIELD].astype(str) == str(g), "participant_id"])
    return comps[comps.participant_id.isin(ids)]

r1 = A.rank_items(subset(G1), items, B=0, compute_se=True)
r2 = A.rank_items(subset(G2), items, B=0, compute_se=True)
diff = A.item_differences(r1, r2)
diff[["label", "rank_a", "rank_b", "theta_a", "theta_b", "diff", "se", "z", "p", "q"]].round(4).head(15)
""")

code(r"""
d = diff.sort_values("diff")
fig, ax = plt.subplots(figsize=(7, 0.28 * len(d) + 1))
colors = [SERIES[0] if q < 0.05 else "#c3c2b7" for q in d.q]
ax.hlines(range(len(d)), d["diff"] - 1.96 * d.se, d["diff"] + 1.96 * d.se, color=colors, lw=2, alpha=.6)
ax.scatter(d["diff"], range(len(d)), color=colors, edgecolor="white", linewidth=1.5, s=50, zorder=3)
ax.axvline(0, color="#c3c2b7", lw=1)
ax.set_yticks(range(len(d)), d.label)
ax.set_xlabel(f"score in {G1} − score in {G2}  (95% CI; blue = q < 0.05)")
ax.set_title(f"Where {G1} and {G2} disagree", loc="left")
plt.tight_layout()
""")

md("## 4. Subgroup structure\nCross two fields to get subgroups, rank within each, then compare every pair of subgroup rankings (Kendall τ). The dendrogram clusters subgroups with similar preference profiles.")

code(r"""
CROSS = FIELDS[:2] if len(FIELDS) >= 2 else FIELDS[:1]
gr = A.rank_by_group(comps, items, demo, CROSS, min_participants=MIN_PARTICIPANTS, orders=ORDERS, B=0)
print("ranked subgroups:", {k: v.n_participants for k, v in gr.groups.items()})
print("too small:", gr.insufficient)
tau = A.rank_correlations(gr)
fig, ax = plt.subplots(figsize=(0.55 * len(tau) + 2, 0.5 * len(tau) + 1.5))
im = ax.imshow(tau.to_numpy(), cmap="RdBu", vmin=-1, vmax=1)
ax.set_xticks(range(len(tau)), tau.columns, rotation=60, ha="right"); ax.set_yticks(range(len(tau)), tau.index)
for i in range(len(tau)):
    for j in range(len(tau)):
        ax.text(j, i, f"{tau.iat[i, j]:.2f}", ha="center", va="center", fontsize=7)
ax.grid(False); plt.colorbar(im, label="Kendall τ"); ax.set_title(f"Rank agreement: {' × '.join(CROSS)}", loc="left")
plt.tight_layout()
""")

code(r"""
if len(tau) >= 3:
    dist = 1 - tau.to_numpy()
    np.fill_diagonal(dist, 0)
    Z = hierarchy.linkage(dist[np.triu_indices(len(dist), 1)], method="average")
    fig, ax = plt.subplots(figsize=(7, 0.3 * len(tau) + 1))
    hierarchy.dendrogram(Z, labels=list(tau.index), orientation="right", ax=ax, color_threshold=0, above_threshold_color=SERIES[0])
    ax.set_xlabel("1 − Kendall τ"); ax.set_title("Subgroups clustered by preference profile", loc="left"); ax.grid(False)
    plt.tight_layout()
""")

code(r"""
# Scores per subgroup, items in overall order: the dashboard's heatmap with numbers you can copy
heat = pd.DataFrame({g: r.table.set_index("label").theta for g, r in gr.groups.items()})
heat = heat.loc[[l for l in dav.table.label if l in heat.index]]
heat.round(2).style.background_gradient(cmap="RdBu", vmin=-2, vmax=2, axis=None)
""")

md("## 5. Diagnostics")

code(r"""
lr = ans[ans.outcome.isin(["left", "right"])]
k = (lr.outcome == "left").sum()
bt = stats.binomtest(k, len(lr), 0.5)
print(f"Position bias: left chosen {k}/{len(lr)} = {k/len(lr):.1%}  (binomial p = {bt.pvalue:.3g}). "
      "Sides are randomised, so bias adds noise but not systematic error.")

fig, axes = plt.subplots(1, 2, figsize=(10, 3))
ms = ans.response_ms.dropna()
axes[0].hist(np.log10(ms.clip(50, 120_000)), bins=40, color=SERIES[0])
axes[0].set_xticks([2, 3, 4, 5], ["0.1s", "1s", "10s", "100s"]); axes[0].set_title("Response time", loc="left")
q = A.quality_flags(comps)
axes[1].scatter(q.median_ms, q.left_share, s=12, c=[SERIES[1] if f else SERIES[0] for f in q.flagged])
axes[1].set_xscale("log"); axes[1].set_xlabel("median ms"); axes[1].set_ylabel("left share")
axes[1].set_title("Participants (orange = flagged)", loc="left")
plt.tight_layout()
""")

code(r"""
# Sensitivity: does excluding flagged participants change the ranking?
if not EXPORT_ZIP:
    _, comps_all, demo_all = A.load_survey(conn, sid, exclude_flagged=False)
    with_flagged = A.rank_items(comps_all, items, B=0).table.set_index("item_id").theta
    without = A.rank_items(comps, items, B=0).table.set_index("item_id").theta
    common = with_flagged.index.intersection(without.index)
    print(f"flagged participants: {int(demo_all.flagged.sum())}  ·  "
          f"Kendall τ(with, without) = {stats.kendalltau(with_flagged[common], without[common]).statistic:.3f}")
""")

md(r"""
## 6. Planning: participants × pairs each

The uncertainty in any gap between two scores has two parts:

$$\operatorname{Var}(\text{gap}) \approx \underbrace{\frac{4(m-1)}{n\,k}}_{\text{comparison noise}} + \underbrace{\frac{2\tau^2\,\text{deff}}{n}\Big(1-\frac{n}{N}\Big)}_{\text{which people you sampled}}$$

for $m$ items, $n$ participants answering $k$ pairs each on average, and a population of $N$. $\tau^2$ is how much one person's scores vary around the population's. Only the first part shrinks with more pairs each; both shrink with more people.

**deff** is the design effect of uneven answering: with unlimited pairs, a few people answer far more than others and pull the ranking towards their tastes, so $n$ people behave like $n/\text{deff}$. It's 1 when everyone answers the same number (a fixed pair count does this).

Both parts come from your data: the model's standard errors measure comparison noise, and the bootstrap over participants measures both. Projecting to another design is then arithmetic, not simulation.

* **min gap**: the smallest true score gap you'd detect with 80% power (α = .05), and the head-to-head win rate it corresponds to.
* **Kendall τ**: expected agreement between the estimated ranking and the one the whole population would give.
* **neighbours ordered**: average chance that two items adjacent in the population ranking come out in the right order.

Scores are shrunk for noise before planning, so the projections lean slightly cautious.
""")

code(r"""
POPULATION = None          # e.g. 5000: size of the population you generalise to (None = very large)
PLAN_PARTICIPANTS = [100, 150, 250, 500, 1000]
PLAN_PAIRS_EACH = [25, 50, 75, 150]
PLAN_BOOT = 500            # tau² needs a steadier bootstrap than the CIs do

plan_fit = A.rank_items(comps, items, ties="davidson", B=PLAN_BOOT, seed=2, compute_se=True)
vp = A.variance_parts(plan_fit)
n0, k0 = vp.n_participants, vp.n_comparisons / vp.n_participants
now = A.plan(vp, n0, k0, N=POPULATION).iloc[0]
print(f"now: {n0} participants × {k0:.0f} answered pairs each on average ({vp.n_comparisons} comparisons)")
print(f"uneven answering: deff = {vp.deff:.2f}, so they count like {n0 / vp.deff:.0f} people")
print(f"between-participant variance τ² = {vp.tau2:.3f}; sampled people are {now.people_share:.0%} of the uncertainty")
print(f"min detectable gap {now.min_gap:.2f} (≈ {now.min_gap_win_pct:.0%} head-to-head) · "
      f"expected Kendall τ {now.kendall_tau:.3f} · neighbours ordered {now.neighbours_ordered:.0%} · "
      f"pairs resolved {now.pairs_resolved:.0%}")

for deff, name in [(None, "answers as uneven as now"), (1.0, "everyone answers the same number")]:
    grid = A.plan(vp, PLAN_PARTICIPANTS, PLAN_PAIRS_EACH, N=POPULATION, deff=deff)
    for col, fmt in [("min_gap_win_pct", "{:.0%}"), ("kendall_tau", "{:.3f}"), ("pairs_resolved", "{:.0%}")]:
        print(f"\n{name} · {col}  (rows: participants, columns: pairs each)")
        display(grid.pivot(index="participants", columns="pairs_each", values=col).style.format(fmt))
""")

code(r"""
ns = np.unique(np.geomspace(max(n0 // 2, 10), 2000, 40).astype(int))
fig, ax = plt.subplots(figsize=(7, 4))
for color, k in zip(SERIES, (25, 75, 150)):
    g = A.plan(vp, ns, k, N=POPULATION)
    ax.plot(g.participants, g.kendall_tau, color=color, lw=2, label=f"{k} pairs each")
g = A.plan(vp, ns, 1e9, N=POPULATION)
ax.plot(g.participants, g.kendall_tau, color="#8a897f", lw=1.5, ls="--", label="unlimited pairs each")
ax.plot([n0], [now.kendall_tau], "o", color="#1f1e1b", ms=7, mec="white", mew=1.5, label="now")
ax.set_xscale("log"); ax.set_xticks([t for t in (20, 50, 100, 200, 500, 1000, 2000) if ns[0] <= t <= ns[-1]])
ax.xaxis.set_major_formatter("{x:.0f}"); ax.xaxis.set_minor_formatter(""); ax.set_xlabel("participants"); ax.set_ylabel("expected Kendall τ vs population")
ax.legend(frameon=False); plt.show()
""")

code(r"""
# Which neighbours can you tell apart? Now vs a target design.
TARGET = (250, 75)  # (participants, pairs each)
lab = items.set_index("id").label
nb = A.neighbour_power(vp, n0, k0, N=POPULATION).merge(
    A.neighbour_power(vp, *TARGET, N=POPULATION)[["rank", "p_right_order", "power"]],
    on="rank", suffixes=("_now", "_target"))
nb.insert(1, "item", nb.item_id.map(lab)); nb.insert(2, "next", nb.next_item_id.map(lab))
nb.drop(columns=["item_id", "next_item_id"]).round(3)
""")

nb = {
    "cells": cells,
    "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                 "language_info": {"name": "python"}},
    "nbformat": 4, "nbformat_minor": 4,
}
out = Path(__file__).with_name("compair_analysis.ipynb")
out.write_text(json.dumps(nb, indent=1, ensure_ascii=False) + "\n")
print("wrote", out)
