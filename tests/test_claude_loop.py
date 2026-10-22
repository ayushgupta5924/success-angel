"""Offline test of the Claude engine's tool loop: a scripted fake client drives the real MCP
server, so the request shape, tool-result plumbing and finalize() are exercised without an
API key.  python -m unittest tests.test_claude_loop -v"""
import asyncio
import json
import unittest
from types import SimpleNamespace as NS

from agent.claude_agent import ClaudeEngine
from agent.session import clinic_session, finalize

M001 = {"id": "M001", "received_at": "2026-11-09T07:02:00-06:00", "channel": "sms",
        "from_name": "Robert Chen", "from_contact": "555-0102",
        "body": "Hi, can I get my lisinopril refilled? Running low. Thanks - Robert Chen"}
M_EMERGENCY = {"id": "T900", "received_at": "2026-11-09T08:00:00-06:00", "channel": "sms",
               "from_name": "Robert Chen", "from_contact": "555-0102",
               "body": "Crushing chest pain since an hour, sweating. Also need my lisinopril."}


def tool_use(i, name, **inp):
    return NS(type="tool_use", id=f"tu_{i}", name=name, input=inp)


def response(*blocks, stop="tool_use"):
    usage = NS(input_tokens=100, output_tokens=50, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return NS(content=list(blocks), stop_reason=stop, usage=usage)


def last_results(messages):
    """tool_result payloads from the most recent user turn, keyed by tool_use id."""
    return {r["tool_use_id"]: json.loads(r["content"]) for r in messages[-1]["content"]}


class FakeMessages:
    """Plays a script of turns. Each turn is a function of the conversation so far, like a model."""

    def __init__(self, script):
        self.script, self.requests = list(script), []

    async def create(self, **kw):
        self.requests.append(kw)
        msgs = kw["messages"]
        # Same contract the API enforces: every tool_use gets a tool_result in the next user turn.
        if len(msgs) > 1 and msgs[-2]["role"] == "assistant":
            used = {b.id for b in msgs[-2]["content"] if b.type == "tool_use"}
            got = {r["tool_use_id"] for r in msgs[-1]["content"]} if isinstance(msgs[-1]["content"], list) else set()
            assert used == got, f"tool_use ids {used} != tool_result ids {got}"
        return self.script.pop(0)(msgs)


def fake_engine(script):
    eng = ClaudeEngine.__new__(ClaudeEngine)
    eng.model, eng.effort = "claude-opus-5-5", "high"
    eng.client = NS(beta=NS(messages=FakeMessages(script)))
    return eng


async def run_one(engine, msg):
    async with clinic_session() as tools:
        tools.start_message()
        decision, usage = await engine.decide(msg, tools)
        return await finalize(msg, decision, tools, engine.name), usage


class ClaudeLoop(unittest.TestCase):
    def test_refill_end_to_end(self):
        seen = {}

        def check(m):
            seen["vid"] = last_results(m)["tu_1"]["verification_id"]
            return response(tool_use(2, "check_refill_eligibility", medication="lisinopril", verification_id=seen["vid"]))

        def prepare(m):
            self.assertEqual(last_results(m)["tu_2"]["status"], "eligible")
            return response(tool_use(3, "prepare_refill_request", medication="lisinopril", message_id="M001",
                                     verification_id=seen["vid"]),
                               tool_use(4, "draft_reply", message_id="M001",
                                     text="Hi Robert, your refill request is with your provider for approval."))

        script = [
            lambda m: response(tool_use(1, "verify_sender", from_name="Robert Chen", from_contact="555-0102")),
            check,
            prepare,
            lambda m: response(tool_use(5, "submit_triage", patient_id="P002", sender_verified=True,
                                        intents=["refill"], urgency="routine",
                                        actions=["prepare_refill_for_approval"], language="English",
                                        rationale="eligible refill")),
        ]
        engine = fake_engine(script)
        res, usage = asyncio.run(run_one(engine, M001))

        self.assertEqual(res["actions"], ["prepare_refill_for_approval"])
        self.assertEqual(res["routed_to"], ["provider_refills"])  # read back from the server, not the model
        self.assertEqual(res["side_effects"]["refills"][0]["status"], "pending_provider_approval")
        self.assertIn("provider for approval", res["reply_draft"])
        self.assertEqual(usage["turns"], 4)
        self.assertEqual(res["tool_errors"], [])

        req = engine.client.beta.messages.requests[0]
        self.assertEqual(req["model"], "claude-opus-5-5")
        self.assertEqual(req["thinking"], {"type": "adaptive"})
        self.assertEqual(req["output_config"], {"effort": "high"})
        self.assertNotIn("tool_choice", req)  # forced tool_choice is a 400 on this model
        submit = next(t for t in req["tools"] if t["name"] == "submit_triage")
        self.assertTrue(submit["strict"])
        self.assertIn("escalate_emergency", {t["name"] for t in req["tools"]})

    def test_guardrail_escalates_when_model_under_triages(self):
        script = [lambda m: response(tool_use(1, "submit_triage", patient_id="P002", sender_verified=True,
                                              intents=["refill"], urgency="routine", actions=["acknowledge"],
                                              language="English", rationale="missed it"))]
        res, _ = asyncio.run(run_one(fake_engine(script), M_EMERGENCY))
        self.assertEqual(res["urgency"], "emergency")
        self.assertIn("clinician_urgent", res["routed_to"])
        self.assertTrue(res["guardrail_notes"])

    def test_refusal_raises_so_runner_falls_back(self):
        engine = fake_engine([lambda m: response(stop="refusal")])

        async def go():  # caught inside the session, as run.py does
            async with clinic_session() as tools:
                tools.start_message()
                with self.assertRaises(RuntimeError):
                    await engine.decide(M001, tools)

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
