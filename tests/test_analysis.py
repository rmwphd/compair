import numpy as np
import pandas as pd
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
