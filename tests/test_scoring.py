from countywatch.scoring import combine_scores, risk_for_signal, sentiment_for_signal


def test_adopted_moratorium_is_high_risk():
    score = risk_for_signal(
        stage="adopted", mechanisms=["moratorium", "ordinance"], posture="restrictive",
        confidence=0.95, activity_date="2026-06-01",
    )
    assert score >= 85


def test_supportive_tax_incentive_is_not_restriction_risk():
    score = risk_for_signal(
        stage="adopted", mechanisms=["tax_incentive", "development_agreement"], posture="supportive",
        confidence=0.9, activity_date="2026-06-01",
    )
    assert score < 25
    assert sentiment_for_signal("supportive", 0.9, "adopted") < 0


def test_aggregate_preserves_strongest_and_is_bounded():
    assert combine_scores([70]) == 70
    combined = combine_scores([70, 55, 40])
    assert 70 <= combined <= 100
