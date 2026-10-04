import random

import numpy as np

from app.pairing import choose_pair
from tests.conftest import simulate_comparisons


def test_no_repeats_and_exhaustion():
    ids = list(range(1, 7))  # 15 possible pairs
    rng = random.Random(1)
    seen = set()
    for _ in range(15):
        pair = choose_pair(ids, {}, seen, lambda a: {}, rng)
        key = (min(pair), max(pair))
        assert key not in seen and pair[0] != pair[1]
        seen.add(key)
    assert choose_pair(ids, {}, seen, lambda a: {}, rng) is None


def test_exposure_balanced_and_graph_connected():
    comps, items, _ = simulate_comparisons(np.zeros(200), n_participants=150, per_participant=20, seed=3)
    counts = np.bincount(np.r_[comps.item_left, comps.item_right], minlength=201)[1:]
    assert counts.min() > 0
    assert counts.std() / counts.mean() < 0.1  # random sampling would give ~0.18 here

    # Connectivity via union-find
    parent = list(range(201))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in zip(comps.item_left, comps.item_right):
        parent[find(a)] = find(b)
    assert len({find(i) for i in range(1, 201)}) == 1


def test_few_repeated_pairs():
    comps, _, _ = simulate_comparisons(np.zeros(100), n_participants=50, per_participant=20, seed=4)
    pairs = {(min(a, b), max(a, b)) for a, b in zip(comps.item_left, comps.item_right)}
    # 1000 comparisons over 4950 possible pairs: nearly all should be distinct.
    assert len(pairs) > 0.97 * len(comps)
