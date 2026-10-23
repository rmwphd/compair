import numpy as np
import pandas as pd
import pytest
from scipy import stats

from app import analysis as A
from tests.conftest import simulate_comparisons


def test_recovers_ranking_and_tie_parameter():
    theta = np.linspace(-2, 2, 30)
    comps, items, _ = simulate_comparisons(theta, n_participants=300, per_participant=30, nu=0.4, seed=0)
    r = A.rank_items(comps, items, B=0)
    est = r.table.set_index("item_id").loc[items.id, "theta"].to_numpy()
    assert stats.spearmanr(est, theta).statistic > 0.95
    assert 0.25 < r.fit.nu < 0.6


def test_half_tie_mode_and_perfect_winner_is_finite():
    comps = pd.DataFrame({
        "participant_id": ["a"] * 4,
        "item_left": [1, 1, 2, 3], "item_right": [2, 3, 3, 1],
        "outcome": ["left", "left", "tie", "right"],
    })
    items = pd.DataFrame({"id": [1, 2, 3], "label": list("xyz")})
    r = A.rank_items(comps, items, ties="half", B=0)
    assert np.isfinite(r.table.theta).all()
    assert r.table.iloc[0].item_id == 1


def test_bootstrap_ci_coverage():
    theta = np.linspace(-1.5, 1.5, 12)
    covered, total = 0, 0
    for seed in range(8):
        comps, items, _ = simulate_comparisons(theta, n_participants=80, per_participant=25, seed=seed)
        r = A.rank_items(comps, items, B=150, seed=seed, l2=0.01)
        t = r.table.set_index("item_id").loc[items.id]
        truth = theta - theta.mean()
        covered += ((t.theta_lo <= truth) & (truth <= t.theta_hi)).sum()
        total += len(truth)
    assert 0.85 < covered / total <= 1.0


def test_group_differences_detected():
    base = np.linspace(-1.5, 1.5, 15)
    flipped = base.copy()
    flipped[[0, 14]] = flipped[[14, 0]]  # groups disagree on the two extreme items
    comps, items, demo = simulate_comparisons(
        base, n_participants=300, per_participant=30, seed=5, group_theta={"A": base, "B": flipped})
    gr = A.rank_by_group(comps, items, demo, ["group"], B=50, compute_se=True)
    assert set(gr.groups) == {"A", "B"}
    lrt = A.interaction_lrt(comps, items, demo, ["group"])
    assert lrt["p_value"] < 1e-6
    diff = A.item_differences(gr.groups["A"], gr.groups["B"])
    assert set(diff.item_id[:2]) == {1, 15}
    assert (diff.q[:2] < 0.01).all()
    tau = A.rank_correlations(gr).loc["A", "B"]
    assert 0.4 < tau < 0.9


def test_no_false_interaction_when_groups_agree():
    base = np.linspace(-1, 1, 10)
    comps, items, demo = simulate_comparisons(
        base, n_participants=200, per_participant=20, seed=6, group_theta={"A": base, "B": base})
    assert A.interaction_lrt(comps, items, demo, ["group"])["p_value"] > 0.01


def test_quality_flags():
    comps = pd.DataFrame({
        "participant_id": ["fast"] * 12 + ["ok"] * 12,
        "item_left": [1] * 24, "item_right": [2] * 24,
        "outcome": ["left"] * 12 + ["left", "right"] * 6,
        "response_ms": [100] * 12 + [2000] * 12,
    })
    q = A.quality_flags(comps).set_index("participant_id")
    assert q.loc["fast", "flagged"] and not q.loc["ok", "flagged"]


def test_group_order_follows_question_order():
    comps, items, demo = simulate_comparisons(np.zeros(5), n_participants=30, per_participant=5, seed=7,
                                              group_theta={"<25": np.zeros(5), "25–34": np.zeros(5), "65+": np.zeros(5)})
    labels = [lab for lab, _, _ in A.split_by(comps, demo, ["group"], {"group": ["<25", "25–34", "65+"]})]
    assert labels == ["<25", "25–34", "65+"]


def _heterogeneous(theta, sd_person, n_participants, per_participant, seed):
    """Each participant has their own scores theta + sd_person * z; pairs drawn uniformly.
    per_participant: an int, or one count per participant."""
    rng = np.random.default_rng(seed)
    m = len(theta)
    ks = np.broadcast_to(per_participant, n_participants)
    pairs = np.array([(a, b) for a in range(m) for b in range(a + 1, m)])
    rows = []
    for p in range(n_participants):
        u = theta + sd_person * rng.standard_normal(m)
        idx = rng.choice(len(pairs), ks[p], replace=False)
        a, b = pairs[idx, 0], pairs[idx, 1]
        win = rng.random(ks[p]) < 1 / (1 + np.exp(-(u[a] - u[b])))
        rows.append(pd.DataFrame({"participant_id": f"p{p}", "item_left": a + 1, "item_right": b + 1,
                                  "outcome": np.where(win, "left", "right")}))
    items = pd.DataFrame({"id": range(1, m + 1), "label": [f"item{i}" for i in range(1, m + 1)]})
    return pd.concat(rows), items


def test_assumed_parts_match_closed_form():
    m, n, k, tau2 = 20, 100, 40, 0.5
    vp = A.assumed_parts(np.zeros(m), tau2)
    row = A.plan(vp, n, k).iloc[0]
    assert row.gap_sd ** 2 == pytest.approx(4 * (m - 1) / (n * k) + 2 * tau2 / n, rel=1e-6)
    assert row.people_share == pytest.approx((2 * tau2 / n) / row.gap_sd ** 2, rel=1e-6)
    assert A.plan(vp, n, k, N=2 * n).iloc[0].gap_sd < row.gap_sd  # finite population helps


def test_variance_parts_recover_between_participant_variance():
    theta = np.linspace(-1.5, 1.5, 20)
    for sd_person, (lo, hi) in [(0.0, (0.0, 0.12)), (1.0, (0.4, 0.8))]:  # tau2 ~ 0.59 on the score scale at sd 1
        comps, items = _heterogeneous(theta, sd_person, 150, 40, seed=3)
        r = A.rank_items(comps, items, ties="half", B=300, seed=3, compute_se=True)
        vp = A.variance_parts(r)
        assert lo <= vp.tau2 <= hi
        # At the current design, the projected uncertainty matches the bootstrap's.
        boot_gap_var = 2 * np.trace(np.cov(r.boot_theta, rowvar=False)) / (len(theta) - 1)
        now = A.plan(vp, vp.n_participants, vp.n_comparisons / vp.n_participants).iloc[0]
        sd = A._gap_sd(vp.cov(vp.n_participants, vp.n_comparisons / vp.n_participants))
        assert (sd[np.triu_indices(len(theta), 1)] ** 2).mean() == pytest.approx(boot_gap_var, rel=0.02)
        assert 0 < now.kendall_tau < 1


def test_uneven_answers_are_a_design_effect_not_disagreement():
    theta = np.linspace(-1.5, 1.5, 20)
    ks = np.where(np.arange(150) % 10 == 0, 150, 25)  # a tenth of people answer 6x as much
    comps, items = _heterogeneous(theta, 1.0, 150, ks, seed=4)
    vp = A.variance_parts(A.rank_items(comps, items, ties="half", B=300, seed=4, compute_se=True))
    assert vp.deff == pytest.approx(150 * np.sum(ks**2) / np.sum(ks) ** 2)
    assert vp.deff > 1.5
    assert 0.4 <= vp.tau2 <= 0.8  # same people-level spread as the even design
    k = ks.mean()
    assert A.plan(vp, 150, k, deff=1.0).iloc[0].gap_sd < A.plan(vp, 150, k).iloc[0].gap_sd


def test_plan_more_people_beats_more_pairs():
    vp = A.assumed_parts(np.random.default_rng(0).normal(0, 1, 30), tau2=0.6)
    g = A.plan(vp, [60, 120, 240], [80, 40, 20]).set_index(["participants", "pairs_each"])
    # Same 4800 comparisons, spread over more people -> smaller gaps detectable, better ranking.
    same_budget = g.loc[[(60, 80), (120, 40), (240, 20)]]
    assert same_budget.min_gap.is_monotonic_decreasing
    assert same_budget.kendall_tau.is_monotonic_increasing
    nb = A.neighbour_power(vp, 120, 40)
    assert len(nb) == 29 and (nb.gap >= 0).all() and nb.p_right_order.between(0.5, 1).all()
