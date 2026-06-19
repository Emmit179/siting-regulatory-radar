from __future__ import annotations

import re

from .models import Passage
from .utils import normalize_space

TOPIC_PATTERNS: dict[str, list[tuple[re.Pattern[str], float]]] = {
    "solar": [
        (re.compile(r"\butility[- ]scale solar\b", re.I), 9),
        (re.compile(r"\bcommercial solar\b", re.I), 8),
        (re.compile(r"\bsolar (?:energy )?(?:facility|farm|project|development|installation|array)s?\b", re.I), 8),
        (re.compile(r"\bphotovoltaic\b", re.I), 7),
        (re.compile(r"\bsolar\b", re.I), 3),
    ],
    "data_center": [
        (re.compile(r"\bdata cent(?:er|re)s?\b", re.I), 9),
        (re.compile(r"\bhyperscale\b", re.I), 8),
        (re.compile(r"\bserver farm\b", re.I), 8),
        (re.compile(r"\bhigh[- ]density computing\b", re.I), 8),
        (re.compile(r"\bAI (?:compute|campus|infrastructure)\b", re.I), 7),
        (re.compile(r"\bcryptocurrency (?:mine|mining|facility)\b", re.I), 5),
    ],
    "bess": [
        (re.compile(r"\bbattery energy storage systems?\b", re.I), 10),
        (re.compile(r"\bBESS\b"), 9),
        (re.compile(r"\benergy storage (?:system|facility|project)s?\b", re.I), 8),
        (re.compile(r"\blithium[- ]ion batter(?:y|ies)\b", re.I), 6),
    ],
    "wind": [
        (re.compile(r"\bwind (?:energy )?(?:farm|facility|project|turbine)s?\b", re.I), 8),
        (re.compile(r"\bwindmill(?:s)?\b", re.I), 4),
    ],
    "general_land_use": [
        (re.compile(r"\bsubdivision regulations?\b", re.I), 4),
        (re.compile(r"\bland[- ]use regulations?\b", re.I), 5),
        (re.compile(r"\bzoning regulations?\b", re.I), 5),
    ],
}

REGULATORY_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    ("moratorium", re.compile(r"\bmoratorium\b|\btemporary (?:halt|pause|suspension)\b", re.I), 10),
    ("prohibition", re.compile(r"\b(?:ban|prohibit|prohibition|not permit|disallow)(?:ed|s|ing)?\b", re.I), 9),
    ("drafting", re.compile(r"\b(?:draft|prepare|develop|write|bring back)\b.{0,80}\b(?:ordinance|regulation|policy|moratorium)\b", re.I | re.S), 9),
    ("staff_direction", re.compile(r"\b(?:direct|instruct|authorize|task)(?:ed|s|ing)?\b.{0,100}\b(?:staff|counsel|attorney|administrator|fire marshal)\b", re.I | re.S), 9),
    ("adoption", re.compile(r"\b(?:adopt|approve|enact|pass)(?:ed|s|ing)?\b.{0,100}\b(?:ordinance|resolution|regulation|moratorium|policy)\b", re.I | re.S), 9),
    ("hearing", re.compile(r"\bpublic hearing\b|\bhearing notice\b", re.I), 7),
    ("ordinance", re.compile(r"\bordinance\b|\bregulation(?:s)?\b", re.I), 6),
    ("zoning", re.compile(r"\bzoning\b|\bland[- ]use\b|\bconditional use\b|\bspecial use\b", re.I), 6),
    ("permitting", re.compile(r"\bpermit(?:ting|s)?\b|\bapplication review\b", re.I), 5),
    ("setbacks", re.compile(r"\bsetback(?:s)?\b|\bbuffer zone\b", re.I), 5),
    ("study", re.compile(r"\b(?:study|workshop|committee|working group|research|review options)\b", re.I), 4),
    ("incentive", re.compile(r"\btax abatement\b|\bchapter 312\b|\bincentive agreement\b|\bdevelopment agreement\b", re.I), 6),
]

CONCERN_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    ("water", re.compile(r"\b(?:water use|groundwater|aquifer|water demand|water supply)\b", re.I), 3),
    ("fire", re.compile(r"\b(?:fire safety|fire code|thermal runaway|emergency response)\b", re.I), 3),
    ("noise", re.compile(r"\bnoise\b|\bdecibel(?:s)?\b", re.I), 2),
    ("roads", re.compile(r"\broad damage\b|\bhaul route\b|\btraffic impact\b", re.I), 2),
    ("decommission", re.compile(r"\bdecommission(?:ing)?\b|\breclamation\b|\bbonding\b", re.I), 3),
    ("glare", re.compile(r"\bglare\b|\bviewshed\b", re.I), 2),
    ("grid", re.compile(r"\bgrid reliability\b|\btransmission capacity\b|\belectric load\b", re.I), 2),
]

PROCESS_PATTERNS = re.compile(
    r"\b(commissioners? court|county judge|county attorney|agenda item|motion|seconded|vote|public comment|"
    r"workshop|regular meeting|special meeting|executive session|order of the court)\b",
    re.I,
)


def _matches(patterns, text: str):
    for item in patterns:
        if len(item) == 2:
            pattern, weight = item
            label = pattern.pattern
        else:
            label, pattern, weight = item
        for match in pattern.finditer(text):
            yield label, match.start(), match.end(), weight


def extract_passages(text: str, max_passages: int = 10, window: int = 1250) -> list[Passage]:
    """High-recall topic/regulatory proximity filter; it never assigns final risk by itself."""
    if not text or len(text) < 40:
        return []
    topic_hits: list[tuple[str, int, int, float, str]] = []
    for topic, patterns in TOPIC_PATTERNS.items():
        for pattern, weight in patterns:
            for match in pattern.finditer(text):
                topic_hits.append((topic, match.start(), match.end(), weight, normalize_space(match.group(0)).lower()))
    if not topic_hits:
        return []

    windows: list[dict[str, object]] = []
    for topic, start, end, topic_weight, topic_term in topic_hits:
        left = max(0, start - window)
        right = min(len(text), end + window)
        chunk = text[left:right]
        regulatory = list(_matches(REGULATORY_PATTERNS, chunk))
        concerns = list(_matches(CONCERN_PATTERNS, chunk))
        process = bool(PROCESS_PATTERNS.search(chunk))
        regulatory_score = sum(hit[3] for hit in regulatory[:8])
        concern_score = sum(hit[3] for hit in concerns[:5])
        # Topic-only mentions are retained at low rank only when government process language is present.
        score = topic_weight + min(24, regulatory_score) + min(7, concern_score) + (4 if process else 0)
        if regulatory_score == 0 and not process:
            continue
        terms = [topic_term] + [str(hit[0]) for hit in regulatory] + [str(hit[0]) for hit in concerns]
        windows.append({
            "start": left,
            "end": right,
            "topics": {topic},
            "score": score,
            "terms": set(terms),
        })

    if not windows:
        return []
    windows.sort(key=lambda item: int(item["start"]))
    merged: list[dict[str, object]] = []
    for current in windows:
        if merged and int(current["start"]) <= int(merged[-1]["end"]) + 250:
            previous = merged[-1]
            previous["end"] = max(int(previous["end"]), int(current["end"]))
            previous["topics"] = set(previous["topics"]) | set(current["topics"])
            previous["terms"] = set(previous["terms"]) | set(current["terms"])
            previous["score"] = max(float(previous["score"]), float(current["score"])) + 1
        else:
            merged.append(current)

    ranked = sorted(merged, key=lambda item: float(item["score"]), reverse=True)[:max_passages]
    ranked.sort(key=lambda item: int(item["start"]))
    passages: list[Passage] = []
    for index, item in enumerate(ranked, start=1):
        start, end = int(item["start"]), int(item["end"])
        # Expand to paragraph boundaries where possible.
        paragraph_start = text.rfind("\n", max(0, start - 300), start)
        paragraph_end = text.find("\n", end, min(len(text), end + 300))
        start = paragraph_start + 1 if paragraph_start >= 0 else start
        end = paragraph_end if paragraph_end >= 0 else end
        passages.append(Passage(
            id=f"P{index}",
            start=start,
            end=end,
            text=text[start:end].strip(),
            topics=sorted(item["topics"]),
            score=round(float(item["score"]), 2),
            matched_terms=sorted(item["terms"]),
        ))
    return passages
