"""Ranking models and group analysis for pairwise comparison data.

Shared by the admin dashboard and analysis/compair_analysis.ipynb.

Conventions
-----------
* A *comparisons* DataFrame has columns: participant_id, item_left, item_right,
  outcome ('left' | 'right' | 'tie' | 'skip' | None). Skips and unanswered rows
  are dropped by `answered()`.
* Scores are Bradley-Terry log-strengths (theta), centred to mean 0 over the
  fitted items. A difference of 1.0 means the higher item is preferred with
  odds e:1 (about 73%) in a head-to-head, ignoring ties.
* Ties use the Davidson (1970) model by default:
      P(i > j) = pi_i / D,  P(tie) = nu * sqrt(pi_i pi_j) / D,
      D = pi_i + pi_j + nu * sqrt(pi_i pi_j)
  With ties="half" a tie counts as half a win for each side instead.
* A small L2 penalty on theta keeps estimates finite when an item never loses
  (or never wins) and when the comparison graph is sparse.
"""

from __future__ import annotations

import io
import itertools
import sqlite3
import zipfile
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import optimize, stats

# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_survey(conn: sqlite3.Connection, survey_id: int, exclude_flagged: bool = False):
    """Return (items, comparisons, demographics) DataFrames for one survey.

    demographics is wide: one row per participant, one column per demographic
    question key holding the *bucket* (the groupable value), plus `flagged`.
    """
    items = pd.read_sql_query(
        "SELECT id, label, description, active FROM items WHERE survey_id = ? ORDER BY id",
        conn,
        params=(survey_id,),
    )
    comps = pd.read_sql_query(
        """SELECT c.participant_id, c.seq, c.item_left, c.item_right, c.outcome,
                  c.response_ms, c.served_at, c.answered_at
           FROM comparisons c WHERE c.survey_id = ? ORDER BY c.id""",
        conn,
        params=(survey_id,),
    )
    parts = pd.read_sql_query(
        "SELECT id AS participant_id, flagged, started_at, finished_at FROM participants WHERE survey_id = ?",
        conn,
        params=(survey_id,),
    )
    answers = pd.read_sql_query(
        """SELECT pa.participant_id, q.key, pa.bucket
           FROM participant_answers pa JOIN demo_questions q ON q.id = pa.question_id
           WHERE q.survey_id = ?""",
        conn,
        params=(survey_id,),
    )
    demo = parts.set_index("participant_id")
    if not answers.empty:
        wide = answers.pivot(index="participant_id", columns="key", values="bucket")
        demo = demo.join(wide, how="left")
    demo = demo.reset_index()
    if exclude_flagged:
        keep = set(demo.loc[demo["flagged"] == 0, "participant_id"])
        comps = comps[comps["participant_id"].isin(keep)]
        demo = demo[demo["participant_id"].isin(keep)]
    return items, comps, demo


def load_export_zip(path_or_bytes):
    """Load (items, comparisons, demographics) from an admin export ZIP."""
    src = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, (bytes, bytearray)) else path_or_bytes
    with zipfile.ZipFile(src) as z:
        items = pd.read_csv(z.open("items.csv"))
        comps = pd.read_csv(z.open("comparisons.csv"))
        demo = pd.read_csv(z.open("participants.csv"))
    comps = comps.rename(columns={"item_left_id": "item_left", "item_right_id": "item_right"})
    items = items.rename(columns={"item_id": "id"})
    return items, comps, demo


def answered(comps: pd.DataFrame) -> pd.DataFrame:
    return comps[comps["outcome"].isin(["left", "right", "tie"])]


# ---------------------------------------------------------------------------
# Model fitting
# ---------------------------------------------------------------------------


@dataclass
class PairData:
    """Comparisons aggregated per unordered item pair (indices into `item_ids`)."""

    item_ids: np.ndarray  # item id for each index
    i: np.ndarray  # pair -> lower index
    j: np.ndarray  # pair -> higher index
    w_ij: np.ndarray  # i beat j
    w_ji: np.ndarray  # j beat i
    t: np.ndarray  # ties
    # Per-row mapping, kept so bootstrap reweighting is cheap.
    row_pair: np.ndarray = field(repr=False, default=None)
    row_kind: np.ndarray = field(repr=False, default=None)  # 0: i won, 1: j won, 2: tie
    row_participant: np.ndarray = field(repr=False, default=None)
    participants: np.ndarray = field(repr=False, default=None)

    def reweighted(self, row_w: np.ndarray) -> "PairData":
        m = len(self.i)
        w = [np.bincount(self.row_pair, weights=row_w * (self.row_kind == k), minlength=m) for k in range(3)]
        return PairData(self.item_ids, self.i, self.j, w[0], w[1], w[2])


def build_pairs(comps: pd.DataFrame, item_ids=None) -> PairData:
    c = answered(comps)
    if item_ids is None:
        item_ids = np.unique(np.concatenate([c["item_left"].to_numpy(), c["item_right"].to_numpy()]))
    item_ids = np.asarray(sorted(item_ids))
    index = {v: k for k, v in enumerate(item_ids)}
    c = c[c["item_left"].isin(index) & c["item_right"].isin(index)]

    left = c["item_left"].map(index).to_numpy()
    right = c["item_right"].map(index).to_numpy()
    lo, hi = np.minimum(left, right), np.maximum(left, right)
    outcome = c["outcome"].to_numpy()
    winner = np.where(outcome == "left", left, np.where(outcome == "right", right, -1))
    kind = np.where(winner == -1, 2, np.where(winner == lo, 0, 1))

    n = len(item_ids)
    key = lo.astype(np.int64) * n + hi
    uniq, row_pair = np.unique(key, return_inverse=True)
    m = len(uniq)
    w = [np.bincount(row_pair, weights=(kind == k).astype(float), minlength=m) for k in range(3)]
    parts, row_participant = np.unique(c["participant_id"].astype(str).to_numpy(), return_inverse=True)
    return PairData(
        item_ids, (uniq // n).astype(int), (uniq % n).astype(int), w[0], w[1], w[2],
        row_pair, kind, row_participant, parts,
    )


@dataclass
class Fit:
    item_ids: np.ndarray
    theta: np.ndarray
    nu: float  # Davidson tie parameter (0 in "half" mode)
    loglik: float  # unpenalised log-likelihood
    n_params: int
    ties: str
    se: np.ndarray | None = None

    def table(self) -> pd.DataFrame:
        df = pd.DataFrame({"item_id": self.item_ids, "theta": self.theta})
        if self.se is not None:
            df["se"] = self.se
        df["rank"] = rank_desc(self.theta)
        return df.sort_values("rank").reset_index(drop=True)


def _nll_grad(params, pd_: PairData, n, ties, l2):
    theta = params[:n]
    a, b = theta[pd_.i], theta[pd_.j]
    if ties == "davidson":
        eta = params[n]
        m = 0.5 * (a + b)
        stack = np.stack([a, b, eta + m])
        logD = np.logaddexp.reduce(stack, axis=0)
        p_i, p_j, p_t = np.exp(a - logD), np.exp(b - logD), np.exp(eta + m - logD)
        w_ij, w_ji, t = pd_.w_ij, pd_.w_ji, pd_.t
        ll = np.sum(w_ij * (a - logD) + w_ji * (b - logD) + t * (eta + m - logD))
    else:
        logD = np.logaddexp(a, b)
        p_i, p_j, p_t = np.exp(a - logD), np.exp(b - logD), 0.0
        w_ij, w_ji, t = pd_.w_ij + 0.5 * pd_.t, pd_.w_ji + 0.5 * pd_.t, 0.0 * pd_.t
        ll = np.sum(w_ij * (a - logD) + w_ji * (b - logD))
    N = w_ij + w_ji + t
    g_a = w_ij + 0.5 * t - N * (p_i + 0.5 * p_t)
    g_b = w_ji + 0.5 * t - N * (p_j + 0.5 * p_t)
    grad = np.zeros_like(params)
    np.add.at(grad, pd_.i, g_a)
    np.add.at(grad, pd_.j, g_b)
    if ties == "davidson":
        grad[n] = np.sum(t - N * p_t)
    grad[:n] -= l2 * theta
    pen_ll = ll - 0.5 * l2 * np.sum(theta**2)
    return -pen_ll, -grad, ll


def fit(pd_: PairData, ties: str = "davidson", l2: float = 0.05, compute_se: bool = False, x0=None) -> Fit:
    """Penalised maximum-likelihood Bradley-Terry / Davidson fit."""
    n = len(pd_.item_ids)
    if ties == "davidson" and pd_.t.sum() == 0:
        ties_eff = "half"  # no ties observed: nu -> 0, equivalent to plain BT
    else:
        ties_eff = ties
    k = n + (1 if ties_eff == "davidson" else 0)
    if x0 is None or len(x0) != k:
        x0 = np.zeros(k)
        if ties_eff == "davidson":
            x0[n] = np.log(max(pd_.t.sum(), 0.5) / max(pd_.w_ij.sum() + pd_.w_ji.sum(), 1.0))
    bounds = [(None, None)] * n + ([(-12.0, 5.0)] if ties_eff == "davidson" else [])
    if n == 0:
        return Fit(pd_.item_ids, np.zeros(0), 0.0, 0.0, 0, ties_eff)
    res = optimize.minimize(
        lambda p: _nll_grad(p, pd_, n, ties_eff, l2)[:2],
        x0, jac=True, method="L-BFGS-B", bounds=bounds,
        options={"maxiter": 2000, "gtol": 1e-7},
    )
    params = res.x
    ll = _nll_grad(params, pd_, n, ties_eff, l2)[2]
    theta = params[:n] - params[:n].mean()
    nu = float(np.exp(params[n])) if ties_eff == "davidson" else 0.0
    se = _standard_errors(params, pd_, n, ties_eff, l2) if compute_se else None
    return Fit(pd_.item_ids, theta, nu, float(ll), n - 1 + (1 if ties_eff == "davidson" else 0), ties_eff, se)


def _standard_errors(params, pd_, n, ties, l2, h=1e-5):
    """SEs from the inverse Hessian (finite differences of the analytic gradient)."""
    k = len(params)
    H = np.zeros((k, k))
    for c in range(k):
        e = np.zeros(k)
        e[c] = h
        H[:, c] = (_nll_grad(params + e, pd_, n, ties, l2)[1] - _nll_grad(params - e, pd_, n, ties, l2)[1]) / (2 * h)
    H = 0.5 * (H + H.T)
    try:
        cov = np.linalg.inv(H)
    except np.linalg.LinAlgError:
        cov = np.linalg.pinv(H)
    # Covariance of the centred theta: C = (I - 11'/n) Cov (I - 11'/n)
    P = np.eye(n) - 1.0 / n
    cov_t = P @ cov[:n, :n] @ P
    return np.sqrt(np.clip(np.diag(cov_t), 0, None))


def rank_desc(x: np.ndarray) -> np.ndarray:
    """Rank 1 = largest. Ties get the minimum rank."""
    return stats.rankdata(-np.asarray(x), method="min").astype(int)


# ---------------------------------------------------------------------------
# Bootstrap and summaries
# ---------------------------------------------------------------------------


@dataclass
class Result:
    fit: Fit
    table: pd.DataFrame  # one row per item, sorted by rank
    n_participants: int
    n_comparisons: int
    boot_theta: np.ndarray | None = None  # (B, n_items)


def bootstrap(pd_: PairData, base: Fit, B: int = 200, seed: int = 0, ties: str = "davidson", l2: float = 0.05):
    """Cluster bootstrap over participants. Returns array (B, n_items) of centred theta."""
    rng = np.random.default_rng(seed)
    P = len(pd_.participants)
    if P < 2:
        return None
    x0 = np.r_[base.theta, np.log(base.nu)] if base.ties == "davidson" else base.theta
    out = np.empty((B, len(pd_.item_ids)))
    for b in range(B):
        counts = np.bincount(rng.integers(0, P, P), minlength=P).astype(float)
        f = fit(pd_.reweighted(counts[pd_.row_participant]), ties=ties, l2=l2, x0=x0)
        out[b] = f.theta
    return out


def item_stats(comps: pd.DataFrame) -> pd.DataFrame:
    c = answered(comps)
    rows = []
    for side, other in (("left", "right"), ("right", "left")):
        d = pd.DataFrame({
            "item_id": c[f"item_{side}"].to_numpy(),
            "win": (c["outcome"] == side).to_numpy(),
            "loss": (c["outcome"] == other).to_numpy(),
            "tie": (c["outcome"] == "tie").to_numpy(),
        })
        rows.append(d)
    d = pd.concat(rows)
    g = d.groupby("item_id").agg(n=("win", "size"), wins=("win", "sum"), losses=("loss", "sum"), ties=("tie", "sum"))
    g["win_pct"] = (g["wins"] + 0.5 * g["ties"]) / g["n"]
    return g.reset_index()


def rank_items(
    comps: pd.DataFrame,
    items: pd.DataFrame,
    ties: str = "davidson",
    B: int = 200,
    seed: int = 0,
    l2: float = 0.05,
    compute_se: bool = False,
) -> Result:
    """Fit + bootstrap + per-item summary for a set of comparisons.

    Only items with at least one answered comparison are ranked; an item with
    no data has no defensible position.
    """
    a = answered(comps)
    seen = np.union1d(a["item_left"].to_numpy(), a["item_right"].to_numpy())
    pd_ = build_pairs(comps, item_ids=np.intersect1d(items["id"].to_numpy(), seen))
    f = fit(pd_, ties=ties, l2=l2, compute_se=compute_se)
    boot = bootstrap(pd_, f, B=B, seed=seed, ties=ties, l2=l2) if B else None

    tab = pd.DataFrame({"item_id": f.item_ids, "theta": f.theta, "rank": rank_desc(f.theta)})
    if f.se is not None:
        tab["se"] = f.se
    if boot is not None:
        tab["theta_lo"], tab["theta_hi"] = np.percentile(boot, [2.5, 97.5], axis=0)
        ranks = np.apply_along_axis(rank_desc, 1, boot)
        tab["rank_lo"], tab["rank_hi"] = np.percentile(ranks, [2.5, 97.5], axis=0).round().astype(int)
    st = item_stats(comps)
    tab = tab.merge(st, on="item_id", how="left").fillna({"n": 0, "wins": 0, "losses": 0, "ties": 0})
    tab = tab.merge(items.rename(columns={"id": "item_id"})[["item_id", "label"]], on="item_id", how="left")
    tab = tab.sort_values(["rank", "label"]).reset_index(drop=True)
    return Result(f, tab, int(a["participant_id"].nunique()), int(len(a)), boot)


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


@dataclass
class GroupResult:
    fields: list[str]
    groups: dict[str, Result]  # group label -> result
    sizes: dict[str, int]  # participants per group (including those below min)
    insufficient: list[str]


def group_label(values) -> str:
    return " · ".join(str(v) for v in values)


def split_by(comps: pd.DataFrame, demo: pd.DataFrame, fields: list[str], orders: dict | None = None):
    """Yield (label, comps_subset, n_participants) for each observed group combination.

    `orders` maps a field to its value order (e.g. age bands low->high); unlisted
    values sort after listed ones, "(no answer)" last.
    """
    orders = orders or {}
    d = demo[["participant_id", *fields]].copy()
    for f in fields:
        d[f] = d[f].fillna("(no answer)").astype(str)
        listed = [str(v) for v in orders.get(f, [])]
        extra = sorted(v for v in d[f].unique() if v not in listed and v != "(no answer)")
        d[f] = pd.Categorical(d[f], categories=listed + extra + ["(no answer)"], ordered=True)
    merged = comps.merge(d, on="participant_id", how="inner")
    for key, sub in merged.groupby(fields, sort=True, observed=True):
        key = key if isinstance(key, tuple) else (key,)
        yield group_label(key), sub, sub["participant_id"].nunique()


def rank_by_group(
    comps: pd.DataFrame,
    items: pd.DataFrame,
    demo: pd.DataFrame,
    fields: list[str],
    min_participants: int = 10,
    orders: dict | None = None,
    **kw,
) -> GroupResult:
    groups, sizes, insufficient = {}, {}, []
    for label, sub, n in split_by(comps, demo, fields, orders):
        sizes[label] = int(n)
        if n < min_participants:
            insufficient.append(label)
            continue
        groups[label] = rank_items(sub, items, **kw)
    return GroupResult(fields, groups, sizes, insufficient)


def rank_correlations(gr: GroupResult) -> pd.DataFrame:
    """Kendall tau between every pair of sufficient groups (on theta)."""
    labels = list(gr.groups)
    M = pd.DataFrame(np.eye(len(labels)), index=labels, columns=labels)
    for a, b in itertools.combinations(labels, 2):
        ta = gr.groups[a].table.set_index("item_id")["theta"]
        tb = gr.groups[b].table.set_index("item_id")["theta"]
        common = ta.index.intersection(tb.index)
        tau = stats.kendalltau(ta[common], tb[common]).statistic if len(common) > 2 else np.nan
        M.loc[a, b] = M.loc[b, a] = tau
    return M


def interaction_lrt(comps: pd.DataFrame, items: pd.DataFrame, demo: pd.DataFrame, fields: list[str],
                    ties: str = "davidson", l2: float = 1e-4, min_participants: int = 1, orders: dict | None = None):
    """Likelihood-ratio test: one shared ranking vs a separate ranking per group.

    H0: all groups share the same item strengths. Uses a tiny penalty so the
    log-likelihoods are near-unpenalised; df counts only items each group
    actually compared. Treat as approximate when groups are small or sparse.
    """
    ids = items["id"].to_numpy()
    subsets = [(lab, sub) for lab, sub, n in split_by(comps, demo, fields, orders) if n >= min_participants]
    if len(subsets) < 2:
        return None
    pooled_comps = pd.concat([s for _, s in subsets])
    pooled = fit(build_pairs(pooled_comps, ids), ties=ties, l2=l2)
    ll_sep, k_sep = 0.0, 0
    for _, sub in subsets:
        pd_ = build_pairs(sub, ids)
        seen = np.union1d(pd_.i, pd_.j)
        f = fit(pd_, ties=ties, l2=l2)
        ll_sep += f.loglik
        k_sep += max(len(seen) - 1, 0) + (1 if f.ties == "davidson" else 0)
    pd_all = build_pairs(pooled_comps, ids)
    k_pool = max(len(np.union1d(pd_all.i, pd_all.j)) - 1, 0) + (1 if pooled.ties == "davidson" else 0)
    stat = 2 * (ll_sep - pooled.loglik)
    dof = max(k_sep - k_pool, 1)
    return {"statistic": float(stat), "df": int(dof), "p_value": float(stats.chi2.sf(stat, dof)),
            "groups": [l for l, _ in subsets]}


def item_differences(a: Result, b: Result) -> pd.DataFrame:
    """Per-item Wald tests of theta_a - theta_b, with Benjamini-Hochberg q-values.

    Needs both results fitted with compute_se=True (falls back to bootstrap SDs).
    Thetas are centred within each group, so a difference means "relative to
    that group's average item".
    """
    def se_of(r: Result):
        if "se" in r.table:
            return r.table.set_index("item_id")["se"]
        if r.boot_theta is not None:
            return pd.Series(r.boot_theta.std(axis=0), index=r.fit.item_ids)
        raise ValueError("need compute_se=True or a bootstrap")

    ta, tb = a.table.set_index("item_id"), b.table.set_index("item_id")
    common = ta.index.intersection(tb.index)
    d = pd.DataFrame({"label": ta.loc[common, "label"], "theta_a": ta.loc[common, "theta"],
                      "theta_b": tb.loc[common, "theta"], "rank_a": ta.loc[common, "rank"],
                      "rank_b": tb.loc[common, "rank"]})
    d["diff"] = d["theta_a"] - d["theta_b"]
    d["se"] = np.sqrt(se_of(a)[common] ** 2 + se_of(b)[common] ** 2)
    d["z"] = d["diff"] / d["se"]
    d["p"] = 2 * stats.norm.sf(np.abs(d["z"]))
    d["q"] = stats.false_discovery_control(d["p"].to_numpy())
    return d.sort_values("p").reset_index()


# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------


def quality_flags(comps: pd.DataFrame, min_answers: int = 10, fast_ms: int = 400, side_bias: float = 0.95) -> pd.DataFrame:
    """Per-participant diagnostics and a `flagged` boolean."""
    a = answered(comps)
    if a.empty:
        return pd.DataFrame(columns=["participant_id", "answers", "median_ms", "left_share", "flagged"])
    g = a.groupby("participant_id")
    out = pd.DataFrame({
        "answers": g.size(),
        "median_ms": g["response_ms"].median(),
        "left_share": g["outcome"].apply(lambda s: (s == "left").sum() / max(((s == "left") | (s == "right")).sum(), 1)),
    })
    enough = out["answers"] >= min_answers
    out["flagged"] = enough & (
        (out["median_ms"] < fast_ms) | (out["left_share"] > side_bias) | (out["left_share"] < 1 - side_bias)
    )
    return out.reset_index()
