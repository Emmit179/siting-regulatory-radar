from countywatch.analyzer import validate_result
from countywatch.models import LLMDocumentResult, Passage


def test_validation_maps_normalized_quote_to_exact_source():
    text = "The Court directed the County Attorney\nto draft a solar moratorium ordinance."
    passage = Passage(id="P1", start=0, end=len(text), text=text, topics=["solar"], score=25, matched_terms=["moratorium"])
    result = LLMDocumentResult.model_validate({
        "document_relevant": True,
        "document_summary": "Direction to draft a solar moratorium.",
        "signals": [{
            "passage_id": "P1",
            "topic": "solar",
            "posture": "restrictive",
            "stage": "drafting",
            "mechanisms": ["moratorium", "ordinance"],
            "title": "Solar moratorium drafting direction",
            "summary": "The court directed counsel to draft a moratorium ordinance.",
            "evidence_quote": "The Court directed the County Attorney to draft a solar moratorium ordinance.",
            "confidence": 0.95,
            "explicit_action": True,
        }],
    })
    signals = validate_result(result, [passage], text, "test", "test-model")
    assert len(signals) == 1
    assert signals[0].evidence_quote == text
    assert signals[0].evidence_start == 0


def test_validation_rejects_hallucinated_quote():
    text = "The Court discussed solar facilities but took no action."
    passage = Passage(id="P1", start=0, end=len(text), text=text, topics=["solar"], score=10, matched_terms=["solar"])
    result = LLMDocumentResult.model_validate({
        "document_relevant": True,
        "signals": [{
            "passage_id": "P1", "topic": "solar", "posture": "restrictive", "stage": "adopted",
            "mechanisms": ["moratorium"], "title": "Adopted moratorium",
            "summary": "A moratorium was adopted.", "evidence_quote": "The Court unanimously adopted a moratorium.",
            "confidence": 0.99, "explicit_action": True,
        }],
    })
    assert validate_result(result, [passage], text, "test", "test-model") == []
