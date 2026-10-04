import random

import numpy as np
import pandas as pd
import pytest

from app import db


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "test.db"
    db.init(path)
    c = db.connect(path)
    yield c
    c.close()


def simulate_comparisons(theta, n_participants=200, per_participant=30, nu=0.3, seed=0, group_theta=None):
    """Synthetic Davidson-model data. group_theta: optional {group: theta_vector}; participants
    are assigned round-robin to groups. Returns (comps, items, demo)."""
    from app.pairing import choose_pair

    rng = np.random.default_rng(seed)
    prng = random.Random(seed)
    n = len(theta)
    ids = list(range(1, n + 1))
    exposure, pair_counts, rows, demo = {}, {}, [], []
    groups = list(group_theta) if group_theta else [None]
    for p in range(n_participants):
        pid = f"p{p}"
        g = groups[p % len(groups)]
        th = np.asarray(group_theta[g] if g else theta)
        demo.append({"participant_id": pid, "group": g, "flagged": 0})
        seen = set()
        for _ in range(per_participant):
            pair = choose_pair(ids, exposure, seen, lambda a: pair_counts.get(a, {}), prng)
            if pair is None:
                break
            l, r = pair
            seen.add((min(l, r), max(l, r)))
            for x, y in ((l, r), (r, l)):
                exposure[x] = exposure.get(x, 0) + 1
                pair_counts.setdefault(x, {})[y] = pair_counts.get(x, {}).get(y, 0) + 1
            pl, pr = np.exp(th[l - 1]), np.exp(th[r - 1])
            pt = nu * np.sqrt(pl * pr)
            u = rng.random() * (pl + pr + pt)
            outcome = "left" if u < pl else ("right" if u < pl + pr else "tie")
            rows.append({"participant_id": pid, "item_left": l, "item_right": r, "outcome": outcome,
                         "response_ms": int(rng.integers(800, 4000))})
    items = pd.DataFrame({"id": ids, "label": [f"item{i}" for i in ids]})
    return pd.DataFrame(rows), items, pd.DataFrame(demo)
