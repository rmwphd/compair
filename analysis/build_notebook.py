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
6. **Planning**: how many more answers for a target precision

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
DB_PATH = Path(os.environ.get("COMPAIR_DB", ROOT / "data" / "compair.db"))  # or set EXPORT_ZIP
EXPORT_ZIP = None                         # e.g. ROOT / "demo-20261004-1200.zip"
SURVEY_SLUG = "demo"
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
    conn = sqlite3.connect(DB_PATH)
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

md("## 6. Planning: how many answers do I need?\nCI half-widths shrink roughly as 1/√n. From the current bootstrap we estimate the answers *per item* needed for a target half-width, both overall and for the smallest group you want to report.")

code(r"""
TARGET_HALF_WIDTH = 0.25   # in score units; 0.25 ≈ distinguishes items ~12 pp apart in head-to-head win rate

t = dav.table.dropna(subset=["theta_lo"])
half_width = ((t.theta_hi - t.theta_lo) / 2).median()
n_per_item = t.n.median()
needed = n_per_item * (half_width / TARGET_HALF_WIDTH) ** 2
print(f"now: median {n_per_item:.0f} answers/item → median CI half-width {half_width:.3f}")
print(f"for ±{TARGET_HALF_WIDTH}: ≈ {needed:.0f} answers/item "
      f"≈ {needed * len(t) / 2:.0f} answered comparisons in total (overall), "
      f"and the same per subgroup you want to report on.")
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
