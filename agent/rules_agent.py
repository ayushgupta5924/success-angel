"""Deterministic baseline engine: keyword intents + the same MCP tools, no LLM.

Purposes: (1) a baseline to compare the LLM against, (2) a degraded-mode fallback if the
LLM API is unavailable, (3) lets the whole pipeline run with no API key. It is expected to be
brittle on phrasing it has not seen; treat its sample-set score with suspicion (it was
written by someone who had read the sample messages).
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from agent import guardrails
from agent.session import ClinicTools

TODAY = date(2026, 11, 9)
WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
            "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2, "jueves": 3, "viernes": 4}
PROVIDERS = {"patel": "PATEL", "okafor": "OKAFOR", "sarah kim": "KIM", "nurse kim": "KIM", "np kim": "KIM"}
SPANISH = re.compile(r"\b(hola|necesito|por favor|cita|tienen|semana|gracias|más|mas)\b", re.I)

INTENT_PATTERNS = {
    "refill": r"refill|send in more|another .{0,20}script|more \w+ ?(please|por favor)|necesito m[aá]s|"
              r"\b(rx|prescription)\b|^\s*\w+ refill",
    "billing": r"\bbalance\b|\bbill(ed|ing)?\b|copay|co-pay|insurance|charge",
    "cancel": r"\bcancel",
    "reschedule": r"\b(move|reschedule|change) (my|the)? ?(appointment|appt|physical|visit)|move my",
    "schedule": r"\bbook|appointment|\bappt\b|\bcita\b|get (in|seen)|be seen|\bseen\b|see dr|want to see|"
                r"\bphysical\b|chequeo|follow-up|follow up",
    "records": r"records?|results|vaccinat|immuni[sz]ation|copy of",
    "admin": r"\bhours\b|\bopen\b|closed|phone number|address|change my (phone|email|address)",
    "clinical_question": r"what (does|do|is) .{0,40}mean|explain|is it normal|should i (take|stop)",
    "thanks": r"^\s*(thanks|thank you)|thanks so much|appreciate",
    "new_patient": r"new patients?|accept(ing)? new",
    "spam": r"unsubscribe|special offer|limited time|reply stop",
}


def _has(intent: str, body: str) -> bool:
    return bool(re.search(INTENT_PATTERNS[intent], body, re.I | re.M))


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def parse_when(text: str, base: date = TODAY) -> dict:
    """Very small date-intent parser for the phrasing patients actually use."""
    t = text.lower()
    out: dict = {"part_of_day": "any"}
    if re.search(r"\bmorning|\bam\b|por la mañana", t):
        out["part_of_day"] = "morning"
    if re.search(r"afternoon|\bpm\b|por la tarde", t) and not re.search(r"\d ?pm", t):
        out["part_of_day"] = "afternoon"
    m = re.search(r"\b(\d{1,2})(?::(\d\d))? ?(am|pm)\b", t)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
        out["time"] = f"{h:02d}:{m.group(2) or '00'}"
    week = None
    if re.search(r"next week|next (monday|tuesday|wednesday|thursday|friday)|la pr[oó]xima semana|semana que viene", t):
        week = _monday(base) + timedelta(days=7)
    elif re.search(r"this week|esta semana", t):
        week = _monday(base)
    day_m = re.search(r"\bthe (\d{1,2})(st|nd|rd|th)\b|\b(\d{1,2})(st|nd|rd|th) of", t)
    wd = next((WEEKDAYS[w] for w in WEEKDAYS if re.search(rf"\b{w}\b", t)), None)
    if re.search(r"today or tomorrow|today", t):
        out["from"], out["to"] = base, base + timedelta(days=1 if "tomorrow" in t else 0)
    elif "tomorrow" in t:
        out["from"] = out["to"] = base + timedelta(days=1)
    elif day_m:
        d = date(base.year, base.month, int(day_m.group(1) or day_m.group(3)))
        out["from"] = out["to"] = d
    elif wd is not None:
        start = week or _monday(base)
        d = start + timedelta(days=wd)
        if d < base:
            d += timedelta(days=7)
        out["from"] = out["to"] = d
    elif week:
        out["from"], out["to"] = week, week + timedelta(days=4)
    return out


def _provider_in(text: str) -> str | None:
    t = text.lower()
    for k, v in PROVIDERS.items():
        if k in t:
            return v
    if re.search(r"\b(dr\.?|doctor) kim\b|with kim\b", t):
        return "KIM"
    return None


def _visit_type(text: str) -> str:
    t = text.lower()
    if re.search(r"physical|chequeo|check-?up anual", t):
        return "annual_physical"
    if re.search(r"follow[- ]?up", t):
        return "follow_up"
    if re.search(r"telehealth|video", t):
        return "telehealth"
    return "sick_visit" if re.search(r"fever|cough|pain|uti|burning|sick|rash|vomit", t) else "follow_up"


class RulesEngine:
    name = "rules"

    async def decide(self, msg: dict, tools: ClinicTools) -> tuple[dict, dict]:
        body, mid = msg.get("body", ""), msg["id"]
        scan = guardrails.scan(body)
        spanish = bool(SPANISH.search(body))
        d = {"patient_id": None, "sender_verified": False, "intents": [], "urgency": "routine",
             "actions": [], "language": "Spanish" if spanish else "English", "rationale": "rules"}
        reply: list[str] = []

        def act(*a):
            d["actions"].extend(x for x in a if x not in d["actions"])

        if _has("spam", body):
            d["intents"], d["actions"] = ["spam"], ["no_action"]
            return d, {}

        # Who is this about?
        patient_hint = None
        names_in_body = await self._names_in_body(body, tools)
        if names_in_body and names_in_body[0]["name"].lower() != msg.get("from_name", "").lower():
            patient_hint = names_in_body[0]["name"]
        v, _ = await tools.call("verify_sender", {
            "from_name": msg.get("from_name", ""), "from_contact": msg.get("from_contact", ""),
            "stated_dob": msg.get("stated_dob"), "patient_name": patient_hint})
        vid = v.get("verification_id") if isinstance(v, dict) else None
        first = msg.get("from_name", "").split()[0] if msg.get("from_name") else "there"
        if vid:
            d.update(patient_id=v["patient_id"], sender_verified=True)
            if v.get("preferred_language") == "Spanish":
                d["language"] = "Spanish"
        else:
            found, _ = await tools.call("find_patient", {"name": patient_hint or msg.get("from_name")})
            if found["matches"]:
                d["patient_id"] = found["matches"][0]["patient_id"]

        # Safety first.
        if scan["urgency_floor"] == "emergency":
            d["intents"], d["urgency"] = ["clinical"], "emergency"
            await tools.call("escalate_emergency", {"message_id": mid, "patient_id": d["patient_id"],
                                                    "summary": body[:300], "language": d["language"]})
            act("escalate_emergency")
            return d, {}

        if not vid:
            if patient_hint:
                d["intents"] = ["third_party"]
                act("decline_disclosure")
                reply.append(f"Hi {first}, for privacy we can't share information about another person unless "
                             "you are an authorised contact on their file. They are welcome to contact us directly.")
            elif d["patient_id"]:
                d["intents"] = self._intents(body) or ["other"]
                act("request_verification")
                reply.append(f"Hi {first}, to protect your privacy we need to verify your identity first. Please "
                             "reply from the phone or email on your file, or confirm your full name and date of birth.")
            elif _has("new_patient", body) or _has("schedule", body):
                d["intents"] = ["new_patient"]
                slots, _ = await tools.call("find_free_slots", {"visit_type": "new_patient",
                                                                "date_from": TODAY.isoformat(), "limit": 3})
                act("answer_directly", "offer_new_patient_booking")
                opts = "; ".join(f"{s['weekday']} {s['date']} {s['start']} with {s['provider_name']}"
                                 for s in slots["slots"][:3])
                reply.append(f"Hi {first}, yes, we are accepting new patients. A new-patient visit is 40 minutes; "
                             f"available times include {opts}. Reply with a time that suits you and we'll book it.")
            else:
                d["intents"] = ["other"]
                act("route_to_front_desk")
                await tools.call("route_to_queue", {"message_id": mid, "queue": "front_desk", "priority": "routine",
                                                    "summary": body[:300]})
                reply.append(f"Hi {first}, thanks for your message. Our front desk will get back to you.")
            await self._reply(tools, mid, reply, d["language"])
            return d, {}

        summary, _ = await tools.call("get_patient_summary", {"verification_id": vid})
        first = summary["name"].split()[0] if v.get("sender_role") == "patient" else first
        intents = self._intents(body)
        if "refill" not in intents and not re.search(r"\b(inhaler|pills?|meds?)\b.{0,20}(isn'?t|not) (helping|working)",
                                                     body, re.I):
            meds, _ = await tools.call("get_medications", {"verification_id": vid})
            if any(m["medication"].split()[0].lower()[:6] in body.lower() for m in meds["active_medications"]):
                intents.insert(0, "refill")  # a medication on their list is named: treat as a refill ask
        if scan["prompt_injection"]:
            await tools.call("flag_suspicious_instruction", {"message_id": mid, "excerpt": body,
                                                             "reason": "instruction-like text in message"})
            act("flag_suspicious_instruction")
            intents = [i for i in intents if i == "refill"] or ["refill"]
        if scan["urgency_floor"] == "urgent":
            d["urgency"] = "urgent"
        d["intents"] = [i if i != "clinical_question" else "clinical" for i in intents] or ["other"]
        if any(f.startswith("mood") for f in scan["urgent_flags"]):
            d["intents"].append("clinical")

        for intent in intents:
            if intent == "refill":
                await self._refill(msg, vid, summary, tools, act, reply, d, all_meds=scan["prompt_injection"])
            elif intent == "billing":
                await tools.call("route_to_queue", {"message_id": mid, "queue": "billing", "priority": "routine",
                                                    "patient_id": d["patient_id"], "summary": body[:300]})
                act("route_to_billing")
                reply.append("Our billing team will look into your question and get back to you.")
            elif intent == "records" or (intent == "admin" and re.search(r"phone number|address|email", body, re.I)):
                await tools.call("route_to_queue", {"message_id": mid, "queue": "front_desk", "priority": "routine",
                                                    "patient_id": d["patient_id"], "summary": body[:300]})
                act("route_to_front_desk")
                reply.append("I've passed this to our front desk team, who will take care of it.")
            elif intent == "admin":
                info, _ = await tools.call("get_clinic_info", {})
                act("answer_directly")
                if re.search(r"thanksgiving|holiday|open (on|the)", body, re.I):
                    reply.append("We're closed on 26 and 27 November (Thanksgiving and the day after), and on 25 "
                                 "December. We reopen on Monday 30 November.")
                else:
                    h = info["hours"]
                    reply.append(f"We're open Monday to Friday, {h['open']} to {h['close']} (closed {h['lunch'][0]}-"
                                 f"{h['lunch'][1]} for lunch), except on public holidays.")
            elif intent == "clinical_question":
                await tools.call("route_to_queue", {"message_id": mid, "queue": "clinician_routine",
                                                    "priority": "routine", "patient_id": d["patient_id"],
                                                    "summary": body[:300]})
                act("route_to_clinician")
                reply.append("That's a great question for your clinician, so I've passed it to them and they'll "
                             "get back to you.")
            elif intent == "cancel":
                await self._cancel(msg, vid, tools, act, reply)
            elif intent == "reschedule":
                await self._reschedule(msg, vid, tools, act, reply)
            elif intent == "schedule":
                await self._schedule(msg, vid, summary, tools, act, reply, d)
            elif intent == "thanks":
                act("acknowledge")
                reply.append("You're very welcome. We're glad we could help!")

        if any(f.startswith("mood") for f in scan["urgent_flags"]):
            await tools.call("route_to_queue", {"message_id": mid, "queue": "clinician_urgent", "priority": "urgent",
                                                "patient_id": d["patient_id"], "summary": body[:300]})
            act("route_to_clinician")
            reply.append("I've also shared how you've been feeling with your care team, and a clinician will "
                         "reach out to you today. If you feel unsafe or have thoughts of harming yourself, call or "
                         "text 988 or call 911.")
        if not d["actions"]:
            act("route_to_front_desk")
            await tools.call("route_to_queue", {"message_id": mid, "queue": "front_desk", "priority": "routine",
                                                "patient_id": d["patient_id"], "summary": body[:300]})
            reply.append("Thanks for your message. Our front desk will get back to you shortly.")
        await self._reply(tools, mid, [f"Hi {first},"] + reply, d["language"])
        return d, {}

    # ---------------------------------------------------------------- helpers
    def _intents(self, body: str) -> list[str]:
        order = ["refill", "billing", "cancel", "reschedule", "schedule", "records", "admin",
                 "clinical_question", "thanks"]
        found = [i for i in order if _has(i, body)]
        if "reschedule" in found or "cancel" in found:
            found = [i for i in found if i != "schedule"]
        if "clinical_question" in found:
            found = [i for i in found if i not in ("records",)]
        return found

    async def _names_in_body(self, body: str, tools: ClinicTools) -> list[dict]:
        out = []
        for m in re.finditer(r"\b([A-Z][a-z]+(?:'[A-Z][a-z]+)?) ([A-Z][a-z']+)\b", body):
            if m.group(1).lower() in ("dr", "sarah") or m.group(0).lower() in PROVIDERS:
                continue
            res, _ = await tools.call("find_patient", {"name": m.group(0)})
            out += res["matches"]
        return out

    async def _reply(self, tools, mid, parts, language):
        text = " ".join(parts)
        if language == "Spanish":
            text = _to_spanish(text)
        await tools.call("draft_reply", {"message_id": mid, "text": text, "language": language})

    async def _refill(self, msg, vid, summary, tools, act, reply, d, all_meds=False):
        meds, _ = await tools.call("get_medications", {"verification_id": vid})
        body = msg["body"].lower()
        names = [m["medication"] for m in meds["active_medications"]]
        wanted = [n for n in names if n.split()[0].lower().split("/")[0][:6] in body]
        if not wanted:
            brands = [b for b in guardrails_brands() if b in body]
            wanted = [n for n in names if any(guardrails_brands()[b] in n.lower() for b in brands)]
        if not wanted and (all_meds or len(names) == 1):
            wanted = names
        if not wanted:
            reply.append("Which medication would you like refilled?")
            return
        for med in wanted:
            e, _ = await tools.call("check_refill_eligibility", {"verification_id": vid, "medication": med})
            status, short = e["status"], med.split()[0]
            if status == "eligible":
                await tools.call("prepare_refill_request", {"verification_id": vid, "medication": med,
                                                            "message_id": msg["id"]})
                act("prepare_refill_for_approval")
                reply.append(f"I've sent your {short} refill request to your provider for approval; we'll let you "
                             "know once it has been reviewed.")
            elif status == "too_early":
                act("refill_too_early")
                reply.append(f"It's a little early to refill {short}. We can prepare the request from "
                             f"{e['earliest_request_date']}; please message us then.")
            elif status == "not_seen_in_12_months":
                act("refill_needs_visit", "offer_appointment", "route_to_provider")
                urgent = bool(re.search(r"out of|ran out|asap", body))
                if urgent:
                    d["urgency"] = "urgent"
                await tools.call("route_to_queue", {"message_id": msg["id"], "queue": "provider_refills",
                                                    "priority": "urgent" if urgent else "routine",
                                                    "patient_id": d["patient_id"],
                                                    "summary": f"{med}: not seen in 12 months, refill needs a visit"})
                prov = summary["primary_provider"]["id"]
                s, _ = await tools.call("find_free_slots", {"visit_type": "follow_up", "provider": prov,
                                                            "date_from": TODAY.isoformat(), "limit": 3})
                opts = "; ".join(f"{x['weekday']} {x['date']} at {x['start']}" for x in s["slots"][:3])
                reply.append(f"Because it's been over a year since your last visit, we need to see you before "
                             f"{short} can be renewed. I've let your provider know. Available times with "
                             f"{summary['primary_provider']['name']}: {opts}. Reply to book one.")
            else:
                act("route_to_provider")
                await tools.call("route_to_queue", {"message_id": msg["id"], "queue": "provider_refills",
                                                    "priority": "routine", "patient_id": d["patient_id"],
                                                    "summary": f"{med}: {status}"})
                reply.append(f"I've passed your {short} request to the prescribing provider to review, and the "
                             "team will be in touch.")

    async def _find_appt(self, msg, vid, tools):
        appts, _ = await tools.call("list_appointments", {"verification_id": vid})
        when = parse_when(msg["body"])
        if "from" in when:
            return [a for a in appts["appointments"] if a["date"] == when["from"].isoformat()
                    and (not re.search(r"physical", msg["body"], re.I) or a["visit_type"] == "annual_physical")]
        return appts["appointments"][:1]

    async def _cancel(self, msg, vid, tools, act, reply):
        hits = await self._find_appt(msg, vid, tools)
        if len(hits) == 1:
            a = hits[0]
            await tools.call("cancel_appointment", {"verification_id": vid, "appointment_id": a["id"],
                                                    "message_id": msg["id"]})
            act("cancel_appointment")
            reply.append(f"Your appointment on {a['weekday']} {a['date']} at {a['start']} with {a['provider_name']} "
                         "has been cancelled.")
        elif hits:  # ambiguous: ask rather than cancel the wrong one
            act("acknowledge")
            reply.append("You have more than one appointment that day: " + "; ".join(
                f"{a['start']} with {a['provider_name']} ({a['visit_type'].replace('_', ' ')})" for a in hits)
                + ". Which one would you like to cancel (or both)?")
        else:
            act("route_to_front_desk")
            reply.append("I couldn't find that appointment; our front desk will follow up.")

    async def _reschedule(self, msg, vid, tools, act, reply):
        body = msg["body"]
        old = await self._find_appt(msg, vid, tools)
        if not old:
            act("route_to_front_desk")
            reply.append("I couldn't find that appointment; our front desk will follow up.")
            return
        a = old[0]
        target = re.split(r"\bto\b", body, maxsplit=1)[-1]
        when = parse_when(target, base=TODAY)
        if re.search(r"following week|week after", target, re.I):
            start = _monday(date.fromisoformat(a["date"])) + timedelta(days=7)
            when.update({"from": start, "to": start + timedelta(days=4)})
        args = {"visit_type": a["visit_type"], "provider": a["provider"], "part_of_day": when["part_of_day"],
                "date_from": when.get("from", TODAY).isoformat(), "limit": 5}
        if when.get("to"):
            args["date_to"] = when["to"].isoformat()
        s, _ = await tools.call("find_free_slots", args)
        for slot in s["slots"]:
            r, err = await tools.call("reschedule_appointment", {
                "verification_id": vid, "appointment_id": a["id"], "new_date": slot["date"],
                "new_start": slot["start"], "message_id": msg["id"]})
            if not err:
                act("reschedule_appointment")
                reply.append(f"Done: your appointment has moved from {a['weekday']} {a['date']} to {slot['weekday']} "
                             f"{slot['date']} at {slot['start']} with {slot['provider_name']}.")
                return
        act("offer_alternatives")
        reply.append("There's no opening in that window; our front desk will offer you other times.")

    async def _schedule(self, msg, vid, summary, tools, act, reply, d):
        body = msg["body"]
        when = parse_when(body)
        named = _provider_in(body)
        vt = _visit_type(body)
        prov = named
        if not prov and vt == "sick_visit":
            prov = summary["primary_provider"]["id"]
        if not prov and summary["preferred_language"] == "Spanish":
            prov = "KIM"
        start_d = when.get("from", TODAY)
        end_d = when.get("to") or (start_d + timedelta(days=1 if vt == "sick_visit" else 13))
        if when.get("time"):
            chk, _ = await tools.call("check_slot", {"provider": prov or summary["primary_provider"]["id"],
                                                     "date": start_d.isoformat(), "start": when["time"],
                                                     "visit_type": vt})
            if not chk["available"]:
                return await self._offer(msg, tools, act, reply, vt, prov, start_d, when, chk["reason"])
            slot = {"provider": prov or summary["primary_provider"]["id"], "date": start_d.isoformat(),
                    "start": when["time"]}
        else:
            args = {"visit_type": vt, "date_from": start_d.isoformat(), "date_to": end_d.isoformat(),
                    "part_of_day": when["part_of_day"], "limit": 5}
            if prov:
                args["provider"] = prov
            s, _ = await tools.call("find_free_slots", args)
            if not s["slots"] and prov and not named:
                args.pop("provider")
                s, _ = await tools.call("find_free_slots", args)
            if not s["slots"]:
                why = "; ".join(sum(s.get("unavailable_days", {}).values(), [])) or "no openings then"
                return await self._offer(msg, tools, act, reply, vt, prov, start_d, when, why)
            slot = s["slots"][0]
        r, err = await tools.call("book_appointment", {
            "verification_id": vid, "provider": slot["provider"], "date": slot["date"], "start": slot["start"],
            "visit_type": vt, "message_id": msg["id"], "reason": body[:200]})
        if err:
            return await self._offer(msg, tools, act, reply, vt, prov, start_d, when, str(r))
        a = r["appointment"]
        act("book_appointment")
        reply.append(f"You're booked for a {vt.replace('_', ' ')} on {a['weekday']} {a['date']} at {a['start']} with "
                     f"{a['provider_name']}.")

    async def _offer(self, msg, tools, act, reply, vt, prov, start_d, when, why):
        args = {"visit_type": vt, "date_from": start_d.isoformat(), "limit": 3,
                "part_of_day": when.get("part_of_day", "any")}
        if prov:
            args["provider"] = prov
        s, _ = await tools.call("find_free_slots", args)
        opts = "; ".join(f"{x['weekday']} {x['date']} at {x['start']} with {x['provider_name']}" for x in s["slots"])
        act("offer_alternatives")
        reply.append(f"That time isn't available ({why}). The nearest options are: {opts}. Reply with the one you'd "
                     "like and we'll book it.")


def guardrails_brands() -> dict:
    from clinic.store import BRAND_NAMES
    return BRAND_NAMES


_ES = [
    ("You're booked for a annual physical on", "Su chequeo anual está reservado para el"),
    ("You're booked for a", "Su cita está reservada:"),
    (" with ", " con "), (" at ", " a las "),
    ("I've sent your", "Enviamos su solicitud de"), (" refill request to your provider for approval; we'll let you "
                                                    "know once it has been reviewed.",
                                                    " a su proveedor para su aprobación; le avisaremos cuando la revise."),
    ("Hi ", "Hola "),
    ("Monday", "lunes"), ("Tuesday", "martes"), ("Wednesday", "miércoles"), ("Thursday", "jueves"),
    ("Friday", "viernes"),
]


def _to_spanish(text: str) -> str:
    for en, es in _ES:
        text = text.replace(en, es)
    return text
