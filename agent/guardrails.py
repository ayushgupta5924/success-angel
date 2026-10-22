"""Deterministic safety layer around the LLM.

The LLM decides most things, but two failure modes are too costly to leave to it alone:
missing an emergency, and answering one with clinical content. This module is a floor,
not a ceiling: it can only raise urgency, never lower it.

The red-flag lists are deliberately high-precision phrases (standard triage red flags), so
idioms like "I'm dying to get in" do not trigger them.
"""
from __future__ import annotations

import re

ACTIONS = {
    "escalate_emergency", "route_to_clinician", "route_to_provider", "route_to_billing",
    "route_to_front_desk", "prepare_refill_for_approval", "refill_needs_visit",
    "refill_too_early", "book_appointment", "reschedule_appointment", "cancel_appointment",
    "offer_appointment", "offer_alternatives", "offer_new_patient_booking",
    "request_verification", "decline_disclosure", "flag_suspicious_instruction",
    "answer_directly", "acknowledge", "no_action",
}
URGENCY_RANK = {"routine": 0, "urgent": 1, "emergency": 2}

# (pattern, label). Matched case-insensitively against the message body.
EMERGENCY_FLAGS = [
    (r"chest (pain|pressure|tightness)|crushing (chest|pain)", "chest pain"),
    (r"(pain|numb\w*|tingl\w*).{0,30}(left )?arm.{0,40}(sweat|dizz|short of breath|nause)", "cardiac pattern"),
    (r"can'?t (breathe|catch my breath)|cannot breathe|can'?t finish (a |my )?sentences?|"
     r"struggling to breathe|gasping|lips? (are |is )?(blue|turning blue)", "breathing difficulty"),
    (r"(fell|fall|hit|bumped).{0,40}head.{0,80}(confus|drows|vomit|unconscious|passed out|not making sense)|"
     r"head.{0,40}(confus|unconscious)", "head injury with neuro signs"),
    (r"face (is )?droop|slurred speech|sudden(ly)? (weak|numb)|can'?t move (my|one|her|his) (arm|leg|side)",
     "stroke signs"),
    (r"(unconscious|unresponsive|passed out|seizure|seizing|won'?t wake)", "altered consciousness"),
    (r"blood (sugar|glucose).{0,25}\b([1-4]\d|5[0-4])\b", "severe hypoglycaemia"),
    (r"(black|dark|tarry|bloody) stools?|blood in (my |the )?stool|vomit\w* blood|coughing (up )?blood",
     "GI / serious bleeding"),
    (r"bleeding (a lot|heavily|won'?t stop|that won'?t stop)|gums.{0,30}bleed\w*.{0,20}a lot", "heavy bleeding"),
    (r"(throat|tongue|lips?) (is |are )?(swelling|swollen|closing)|anaphyla", "anaphylaxis"),
    (r"(kill myself|suicid|\bend (my life|it all)|want to die|better off dead|hurt myself|overdos)",
     "self-harm risk"),
    (r"severe (abdominal|belly|stomach) pain|worst headache", "severe pain red flag"),
]
URGENT_FLAGS = [
    (r"(don'?t|do not|can'?t) see the point|hopeless|feeling (really )?(low|down|depressed)|"
     r"no reason to (live|go on)", "mood / possible passive suicidal ideation"),
    (r"fever.{0,40}(\b10[1-5]\b|two days|2 days|three days|3 days)|\b10[1-5](\.\d)? ?(f|degrees)?\b.{0,20}fever",
     "persistent fever"),
    (r"out of (pills|medication|meds|my)|ran out|run out", "out of medication"),
    (r"\b(uti|burning when i pee|burning urination)\b", "acute infection symptoms"),
    (r"bleed", "bleeding mention"),
]
INJECTION = re.compile(
    r"ignore (all |your |the )?(previous|prior|above) instructions|system prompt|"
    r"you are now|approve (all|every)|bulk[- ]approve|disregard (your|the) (rules|policy)",
    re.I,
)


def scan(body: str) -> dict:
    """Return red flags found in a message body."""
    text = body or ""
    em = [label for pat, label in EMERGENCY_FLAGS if re.search(pat, text, re.I)]
    ur = [label for pat, label in URGENT_FLAGS if re.search(pat, text, re.I)]
    floor = "emergency" if em else "urgent" if ur and _urgent_counts(ur) else "routine"
    return {"emergency_flags": em, "urgent_flags": ur, "urgency_floor": floor,
            "prompt_injection": bool(INJECTION.search(text))}


def _urgent_counts(labels: list[str]) -> bool:
    # "bleeding mention" and "out of medication" are hints for the model, not a floor by themselves.
    return any(l not in ("bleeding mention", "out of medication") for l in labels)


def max_urgency(a: str, b: str) -> str:
    return a if URGENCY_RANK.get(a, 0) >= URGENCY_RANK.get(b, 0) else b


def clean_actions(actions: list[str]) -> list[str]:
    seen, out = set(), []
    for a in actions:
        if a in ACTIONS and a not in seen:
            seen.add(a)
            out.append(a)
    return out or ["no_action"]
