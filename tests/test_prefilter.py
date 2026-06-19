from countywatch.prefilter import extract_passages
from countywatch.rules import analyze_with_rules


def test_prefilter_finds_early_solar_moratorium_direction():
    text = """
    COMMISSIONERS COURT REGULAR MEETING
    Agenda Item 12. Discuss and consider authorizing the County Attorney to draft a 180-day
    moratorium on applications for utility-scale solar energy facilities while the Court studies
    setbacks, road impacts, and decommissioning requirements. Commissioner Smith moved to authorize
    counsel to prepare the ordinance for consideration at the next meeting. The motion was seconded.
    """
    passages = extract_passages(text)
    assert passages
    assert "solar" in passages[0].topics
    assert "moratorium" in passages[0].matched_terms
    signals = analyze_with_rules(passages)
    assert signals
    signal = signals[0]
    assert signal.topic == "solar"
    assert signal.stage in {"drafting", "staff_direction"}
    assert "moratorium" in signal.mechanisms
    assert signal.evidence_quote in text


def test_prefilter_rejects_unrelated_it_and_rooftop_procurement():
    text = """
    Approve purchase of two server racks for the Sheriff's office evidence system.
    Approve replacement rooftop solar lights at the county park parking lot.
    """
    assert extract_passages(text) == []


def test_data_center_water_hearing_passage():
    text = """
    PUBLIC HEARING: proposed land-use regulations for hyperscale data centers, including groundwater
    demand reporting, noise limits, and emergency-generator permitting. The Commissioners Court will
    receive public comment and may direct staff to prepare an ordinance.
    """
    passages = extract_passages(text)
    assert any("data_center" in passage.topics for passage in passages)


def test_rules_do_not_treat_agenda_request_to_approve_as_adopted():
    text = (
        "Discussion and possible action to approve an ordinance establishing "
        "regulations for utility-scale solar facilities."
    )
    passages = extract_passages(text)
    signals = analyze_with_rules(passages)
    assert signals
    assert all(signal.stage != "adopted" for signal in signals)
