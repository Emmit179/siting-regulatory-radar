from __future__ import annotations

import re

from .models import Passage, ValidatedSignal

MECHANISMS = {
    "moratorium": re.compile(r"\bmoratorium\b|\btemporary (?:halt|pause|suspension)\b", re.I),
    "prohibition": re.compile(r"\b(?:ban|prohibit|prohibition|disallow)(?:ed|s|ing)?\b", re.I),
    "zoning": re.compile(r"\bzoning\b|\bland[- ]use\b|\bconditional use\b|\bspecial use\b", re.I),
    "ordinance": re.compile(r"\bordinance\b|\bregulation(?:s)?\b", re.I),
    "permitting": re.compile(r"\bpermit(?:ting|s)?\b|\bapplication review\b", re.I),
    "setbacks": re.compile(r"\bsetback(?:s)?\b|\bbuffer zone\b", re.I),
    "fire_safety": re.compile(r"\bfire (?:safety|code|marshal)\b|\bthermal runaway\b", re.I),
    "noise": re.compile(r"\bnoise\b|\bdecibel(?:s)?\b", re.I),
    "water": re.compile(r"\b(?:groundwater|aquifer|water use|water demand|water supply)\b", re.I),
    "roads": re.compile(r"\b(?:road damage|haul route|traffic impact)\b", re.I),
    "decommissioning": re.compile(r"\bdecommission(?:ing)?\b|\breclamation\b|\bbonding\b", re.I),
    "tax_incentive": re.compile(r"\btax abatement\b|\bchapter 312\b|\bincentive agreement\b", re.I),
    "development_agreement": re.compile(r"\bdevelopment agreement\b", re.I),
}

STAGES = [
    ("rescinded", re.compile(r"\b(?:rescind|repeal|lift|terminate)(?:ed|s|ing)?\b.{0,100}\b(?:moratorium|ordinance|restriction)\b", re.I | re.S)),
    ("enforcement", re.compile(r"\b(?:enforce|violation|penalty|cease and desist|compliance order)\b", re.I)),
    (
        "adopted",
        re.compile(
            r"(?:\b(?:motion|court|commissioners? court|vote|order)\b.{0,140}"
            r"\b(?:adopted|approved|enacted|passed)\b.{0,140}"
            r"\b(?:ordinance|resolution|moratorium|regulation|policy)\b)"
            r"|(?:\b(?:ordinance|resolution|moratorium|regulation|policy)\b.{0,140}"
            r"\b(?:was|is hereby)\s+(?:adopted|approved|enacted|passed)\b)",
            re.I | re.S,
        ),
    ),
    ("introduction", re.compile(r"\b(?:introduce|first reading|consider adoption|proposed ordinance)\b", re.I)),
    ("public_hearing", re.compile(r"\bpublic hearing\b", re.I)),
    ("public_notice", re.compile(r"\bpublic notice\b|\bnotice is hereby given\b", re.I)),
    ("drafting", re.compile(r"\b(?:draft|prepare|develop|write|bring back)(?:ed|s|ing)?\b.{0,100}\b(?:ordinance|regulation|policy|moratorium)\b", re.I | re.S)),
    ("staff_direction", re.compile(r"\b(?:direct|instruct|authorize|task)(?:ed|s|ing)?\b.{0,100}\b(?:staff|counsel|attorney|administrator|fire marshal)\b", re.I | re.S)),
    ("study", re.compile(r"\b(?:study|workshop|committee|working group|research|review options)\b", re.I)),
]

RESTRICTIVE = re.compile(
    r"\b(moratorium|ban|prohibit|restriction|restrictive|halt|pause|suspend|setback|buffer|deny|denial|"
    r"limit|cap|zoning ordinance|special use permit|conditional use permit)\b",
    re.I,
)
SUPPORTIVE = re.compile(
    r"\b(tax abatement|chapter 312|incentive|support(?:ed|s|ing)?|welcome|economic development|"
    r"development agreement|approve project|job creation|investment)\b",
    re.I,
)
EXPLICIT = re.compile(r"\b(motion|voted?|approved?|adopted?|directed?|authorized?|ordered?|shall|must)\b", re.I)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?;])\s+|\n+", text)
    return [part.strip() for part in parts if len(part.strip()) >= 20]


def _evidence(passage: Passage, topic: str, stage_pattern: re.Pattern[str] | None) -> tuple[str, int, int]:
    topic_terms = {
        "solar": re.compile(r"\bsolar|photovoltaic\b", re.I),
        "data_center": re.compile(r"\bdata cent(?:er|re)|hyperscale|server farm|AI campus\b", re.I),
        "bess": re.compile(r"\bBESS|battery energy storage|energy storage system|lithium[- ]ion\b", re.I),
        "wind": re.compile(r"\bwind (?:farm|facility|project|turbine)|windmill\b", re.I),
        "general_land_use": re.compile(r"\bzoning|land[- ]use|subdivision regulation\b", re.I),
    }[topic]
    candidates = _sentences(passage.text)
    ranked: list[tuple[int, str]] = []
    for sentence in candidates:
        rank = 0
        if topic_terms.search(sentence):
            rank += 5
        if stage_pattern and stage_pattern.search(sentence):
            rank += 5
        if RESTRICTIVE.search(sentence) or SUPPORTIVE.search(sentence):
            rank += 3
        if EXPLICIT.search(sentence):
            rank += 2
        ranked.append((rank, sentence))
    quote = max(ranked, default=(0, passage.text[:500]), key=lambda item: item[0])[1]
    if len(quote) > 700:
        quote = quote[:700].rsplit(" ", 1)[0]
    local = passage.text.find(quote)
    if local < 0:
        local = 0
        quote = passage.text[:700]
    return quote, passage.start + local, passage.start + local + len(quote)


def analyze_with_rules(passages: list[Passage]) -> list[ValidatedSignal]:
    """Conservative, exact-quote fallback for runs without an available LLM."""
    output: list[ValidatedSignal] = []
    seen: set[tuple[str, str, int]] = set()
    for passage in passages:
        mechanisms = [name for name, pattern in MECHANISMS.items() if pattern.search(passage.text)]
        if not mechanisms:
            mechanisms = ["other"]
        stage = "mention"
        stage_pattern: re.Pattern[str] | None = None
        for candidate, pattern in STAGES:
            if pattern.search(passage.text):
                stage, stage_pattern = candidate, pattern
                break
        restrictive = bool(RESTRICTIVE.search(passage.text))
        supportive = bool(SUPPORTIVE.search(passage.text))
        posture = "mixed" if restrictive and supportive else "restrictive" if restrictive else "supportive" if supportive else "neutral"
        # Topic + process language alone is not enough for a deterministic risk signal.
        if stage == "mention" and posture == "neutral" and "other" in mechanisms:
            continue
        for topic in passage.topics:
            quote, start, end = _evidence(passage, topic, stage_pattern)
            key = (topic, stage, start)
            if key in seen:
                continue
            seen.add(key)
            confidence = 0.52
            confidence += 0.07 if stage != "mention" else 0
            confidence += 0.06 if "moratorium" in mechanisms or "prohibition" in mechanisms else 0
            confidence += 0.05 if EXPLICIT.search(passage.text) else 0
            confidence = min(0.72, confidence)
            topic_label = {
                "solar": "solar facilities",
                "data_center": "data centers",
                "bess": "battery storage",
                "wind": "wind facilities",
                "general_land_use": "land use",
            }[topic]
            title = f"{stage.replace('_', ' ').title()} signal involving {topic_label}"
            summary = (
                f"The official record contains a {posture} {stage.replace('_', ' ')} indicator "
                f"for {topic_label}. This rules-based fallback is intentionally conservative and should be read with the quoted evidence."
            )
            output.append(ValidatedSignal(
                topic=topic,
                posture=posture,
                stage=stage,
                mechanisms=mechanisms,
                title=title,
                summary=summary,
                evidence_quote=quote,
                evidence_start=start,
                evidence_end=end,
                confidence=confidence,
                explicit_action=bool(EXPLICIT.search(passage.text) and stage not in {"mention", "study"}),
                authority_caveat="This record shows county discussion or action; it does not by itself establish the county's legal authority.",
                passage_id=passage.id,
                engine="rules",
                provider="local",
                model="regulatory-rules-v1",
            ))
    return output
