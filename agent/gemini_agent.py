"""Gemini-driven engine: the same tool loop, prompt and output schema as the Claude engine, over
Google's OpenAI-compatible endpoint. Uses the free AI Studio tier (GEMINI_API_KEY), so a full run costs $0."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time

import httpx

from agent.claude_agent import MAX_TURNS, SUBMIT_TOOL, SYSTEM_PROMPT
from agent.session import ClinicTools

MODEL = "gemini-3.5-flash-lite"
URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
MAX_RETRIES = 8


def _clean_schema(s):
    """Reduce JSON Schema to the subset Gemini's function declarations accept."""
    if isinstance(s, list):
        return [_clean_schema(x) for x in s]
    if not isinstance(s, dict):
        return s
    out = {}
    for k, v in s.items():
        if k in ("title", "additionalProperties", "default", "$schema", "strict"):
            continue
        if k == "type" and isinstance(v, list):  # ["string", "null"] -> "string", nullable
            non_null = [t for t in v if t != "null"]
            out["type"] = non_null[0] if non_null else "string"
            if "null" in v:
                out["nullable"] = True
            continue
        if k == "anyOf":  # Optional[X] from pydantic: anyOf [X, null] -> X, nullable
            non_null = [x for x in v if x.get("type") != "null"]
            if len(non_null) == 1:
                out.update(_clean_schema(non_null[0]))
                if len(non_null) < len(v):
                    out["nullable"] = True
                continue
        out[k] = _clean_schema(v)
    return out


def _tool_defs(tools: ClinicTools) -> list[dict]:
    defs = [(t.name, t.description or "", t.inputSchema) for t in tools.tools]
    defs.append((SUBMIT_TOOL["name"], SUBMIT_TOOL["description"], SUBMIT_TOOL["input_schema"]))
    return [{"type": "function", "function": {"name": n, "description": d, "parameters": _clean_schema(p)}}
            for n, d, p in defs]


class GeminiEngine:
    name = "gemini"

    def __init__(self, model: str = MODEL, effort: str = "high"):
        self.model = model
        # OpenAI-style reasoning_effort; Gemini maps it to a thinking budget. xhigh/max -> high.
        self.effort = {"xhigh": "high", "max": "high"}.get(effort, effort)
        self.client = httpx.AsyncClient(timeout=180, headers={
            "Authorization": f"Bearer {os.environ['GEMINI_API_KEY']}"})

    async def _post(self, body: dict) -> dict:
        """POST with backoff on free-tier rate limits (429) and transient 5xx."""
        for attempt in range(MAX_RETRIES):
            r = await self.client.post(URL, json=body)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                m = re.search(r'"retryDelay":\s*"(\d+)', r.text)
                wait = int(m.group(1)) + 1 if m else min(10 * 2 ** attempt, 90)
                if "PerDay" in r.text:  # daily quota gone: stop the run rather than sleep for hours or
                    # silently degrade every remaining message to the rules fallback
                    raise SystemExit(f"Gemini daily free-tier quota for {self.model} is used up "
                                     f"(resets in ~{wait // 3600}h). Try another --model or rerun later.")
                print(f"  gemini {r.status_code}, retrying in {wait}s", file=sys.stderr)
                await asyncio.sleep(wait)
                continue
            raise RuntimeError(f"gemini HTTP {r.status_code}: {r.text[:500]}")
        raise RuntimeError("unreachable")

    async def decide(self, msg: dict, tools: ClinicTools) -> tuple[dict, dict]:
        envelope = {k: v for k, v in msg.items() if k != "body"}
        user = (f"New inbox message (message_id {msg['id']}). Metadata:\n{json.dumps(envelope, ensure_ascii=False)}\n\n"
                f"<message_body>\n{msg['body']}\n</message_body>")
        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
        tool_defs = _tool_defs(tools)
        usage = {"input_tokens": 0, "output_tokens": 0, "turns": 0}
        t0 = time.time()
        decision = None
        for _ in range(MAX_TURNS):
            resp = await self._post({"model": self.model, "messages": messages, "tools": tool_defs,
                                     "reasoning_effort": self.effort})
            usage["turns"] += 1
            u = resp.get("usage") or {}
            usage["input_tokens"] += u.get("prompt_tokens", 0) or 0
            usage["output_tokens"] += u.get("completion_tokens", 0) or 0
            choice = resp["choices"][0]
            reply = choice["message"]
            # Echo the assistant turn back unchanged: it carries Gemini's thought signatures.
            messages.append(reply)
            calls = reply.get("tool_calls") or []
            if not calls:
                if choice.get("finish_reason") == "content_filter":
                    raise RuntimeError("gemini blocked the response (content_filter)")
                messages.append({"role": "user", "content": "Finish by calling submit_triage."})
                continue
            for c in calls:
                fn = c["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = None
                if args is None:
                    content, is_err = "error: arguments were not valid JSON", True
                elif fn["name"] == "submit_triage":
                    decision, content, is_err = args, "recorded", False
                else:
                    data, is_err = await tools.call(fn["name"], args)
                    content = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": ("ERROR: " if is_err else "") + content})
            if decision is not None:
                break
        if decision is None:
            raise RuntimeError(f"no submit_triage after {MAX_TURNS} turns")
        usage["cost_usd"] = 0.0  # free tier
        usage["seconds"] = round(time.time() - t0, 1)
        usage["model"] = self.model
        return decision, usage
