"""MCP client session plus the shared post-processing every engine goes through."""
from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent import guardrails

ROOT = Path(__file__).resolve().parent.parent
HARNESS_TOOLS = {"get_message_outcome", "get_run_state"}


class ClinicTools:
    """Calls MCP tools and records every call made while handling one message."""

    def __init__(self, session: ClientSession, tools):
        self.session = session
        self.tools = [t for t in tools if t.name not in HARNESS_TOOLS]
        self.log: list[dict] = []

    def start_message(self) -> None:
        self.log = []

    async def call(self, name: str, args: dict, by: str = "agent") -> tuple[dict | str, bool]:
        res = await self.session.call_tool(name, args)
        text = "".join(getattr(c, "text", "") for c in res.content)
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            data = text
        if name not in HARNESS_TOOLS:
            self.log.append({"tool": name, "args": args, "error": bool(res.isError), "by": by})
        return data, bool(res.isError)

    async def harness(self, name: str, args: dict | None = None) -> dict:
        data, _ = await self.call(name, args or {}, by="harness")
        return data


@asynccontextmanager
async def clinic_session():
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "clinic.server"], cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            yield ClinicTools(session, tools)


async def finalize(msg: dict, decision: dict, tools: ClinicTools, engine: str) -> dict:
    """Merge the engine's decision with what actually happened in the clinic system, and
    apply the deterministic safety floor."""
    scan = guardrails.scan(msg.get("body", ""))
    notes: list[str] = []
    actions = guardrails.clean_actions(decision.get("actions", []))
    urgency = decision.get("urgency") if decision.get("urgency") in guardrails.URGENCY_RANK else "routine"
    patient_id = decision.get("patient_id")
    language = decision.get("language") or "English"
    mid = msg["id"]

    floor = scan["urgency_floor"]
    if guardrails.URGENCY_RANK[floor] > guardrails.URGENCY_RANK[urgency]:
        notes.append(f"guardrail raised urgency {urgency} -> {floor} ({scan['emergency_flags'] or scan['urgent_flags']})")
        urgency = floor

    outcome = await tools.harness("get_message_outcome", {"message_id": mid})
    if urgency == "emergency" and not outcome["escalated"]:
        await tools.call("escalate_emergency", {
            "message_id": mid, "patient_id": patient_id, "language": language,
            "summary": f"[guardrail] possible emergency: {', '.join(scan['emergency_flags']) or 'model-assessed'}. "
                       f"Message: {msg.get('body', '')[:300]}"}, by="guardrail")
        notes.append("guardrail escalated emergency")
    if urgency == "emergency" and "escalate_emergency" not in actions:
        actions.insert(0, "escalate_emergency")

    if ("mood / possible passive suicidal ideation" in scan["urgent_flags"] and urgency != "emergency"
            and not any(q.startswith("clinician") for q in outcome["routed_to"])):
        await tools.call("route_to_queue", {
            "message_id": mid, "queue": "clinician_urgent", "priority": "urgent", "patient_id": patient_id,
            "summary": f"[guardrail] mood concern, needs same-day clinician review: {msg.get('body', '')[:300]}"},
            by="guardrail")
        notes.append("guardrail routed mood concern to clinician_urgent")
        if "route_to_clinician" not in actions:
            actions.append("route_to_clinician")

    if scan["prompt_injection"] and not outcome["flags"]:
        await tools.call("flag_suspicious_instruction", {
            "message_id": mid, "excerpt": msg.get("body", ""), "reason": "[guardrail] instruction-like text"},
            by="guardrail")
        notes.append("guardrail flagged suspicious instruction")
    if scan["prompt_injection"] and "flag_suspicious_instruction" not in actions:
        actions.append("flag_suspicious_instruction")

    outcome = await tools.harness("get_message_outcome", {"message_id": mid})
    if "no_action" in actions and len(actions) > 1:
        actions.remove("no_action")
    reply = (outcome["reply"] or {}).get("text")
    if reply is None and "no_action" not in actions:
        notes.append("no reply drafted")

    return {
        "id": mid,
        "patient_id": patient_id,
        "intents": decision.get("intents", []),
        "urgency": urgency,
        "actions": actions,
        "routed_to": outcome["routed_to"],
        "tool_calls": [c["tool"] for c in tools.log],
        "reply_draft": reply,
        # Extra fields (not in the required format) for review and evaluation.
        "sender_verified": decision.get("sender_verified"),
        "rationale": decision.get("rationale", ""),
        "engine": engine,
        "guardrail_notes": notes,
        "tool_errors": [c for c in tools.log if c["error"]],
        "side_effects": {k: outcome[k] for k in ("refills", "appointments", "flags")},
    }
