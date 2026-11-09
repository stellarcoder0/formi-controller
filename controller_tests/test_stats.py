from controller.stats import p_greater, p_rate_above, step_towards, tv_distance


def test_two_prop_direction():
    assert p_greater(330, 1000, 280, 1000) < 0.01
    assert p_greater(280, 1000, 330, 1000) > 0.99
    assert 0.4 < p_greater(300, 1000, 300, 1000) < 0.6


def test_rate_above_baseline():
    assert p_rate_above(16, 300, 0.015) < 1e-4  # a hallucinating prompt
    assert p_rate_above(5, 300, 0.015) > 0.1    # normal noise


def test_step_respects_max_shift():
    cur, target = {"D1": 0.5, "D2": 0.5}, {"D1": 0.5, "D3": 0.5}
    s = step_towards(cur, target, 0.2)
    assert abs(tv_distance(cur, s) - 0.2) < 1e-4
    assert step_towards(cur, target, 0.9) == {"D1": 0.5, "D3": 0.5}
