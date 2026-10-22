"""Process inbox messages with the agent.

  python run.py                                  # Claude engine, all 40 sample messages
  python run.py --engine rules                   # offline baseline, no API key needed
  python run.py --only M003,M017                 # a subset
  python run.py --messages my_messages.json      # any file in the same format
  python run.py --text "Can I book a physical?" --from-name "Linda Morales" --from-contact 555-0101
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from agent.rules_agent import RulesEngine
from agent.session import clinic_session, finalize

ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env support (KEY=value lines) so the API key can live outside the shell."""
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_messages(args) -> list[dict]:
    if args.text:
        return [{"id": args.id, "received_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "channel": args.channel, "from_name": args.from_name, "from_contact": args.from_contact,
                 "body": args.text, **({"stated_dob": args.dob} if args.dob else {})}]
    msgs = json.loads(Path(args.messages).read_text(encoding="utf-8"))
    if args.only:
        keep = set(args.only.split(","))
        msgs = [m for m in msgs if m["id"] in keep]
    return msgs


def make_engine(name: str, effort: str, model: str | None):
    if name == "rules":
        return RulesEngine()
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("No ANTHROPIC_API_KEY found (env or .env). Set one, or use --engine rules for the offline baseline.")
    from agent.claude_agent import MODEL, ClaudeEngine
    return ClaudeEngine(model=model or MODEL, effort=effort)


async def main(args) -> int:
    engine = make_engine(args.engine, args.effort, args.model)
    fallback = RulesEngine() if args.engine == "claude" and not args.no_fallback else None
    messages = load_messages(args)
    results, totals = [], {"cost_usd": 0.0, "seconds": 0.0, "turns": 0, "degraded": 0}
    async with clinic_session() as tools:
        for msg in messages:
            tools.start_message()
            used = engine.name
            try:
                decision, usage = await engine.decide(msg, tools)
            except Exception as e:  # LLM outage, refusal, loop limit: degrade, never drop a message
                if not fallback:
                    raise
                print(f"[{msg['id']}] {engine.name} failed ({type(e).__name__}: {e}); using rules fallback",
                      file=sys.stderr)
                if args.verbose:
                    traceback.print_exc()
                decision, usage = await fallback.decide(msg, tools)
                used, usage = f"rules (fallback from {engine.name})", {"error": f"{type(e).__name__}: {e}"}
                totals["degraded"] += 1
            res = await finalize(msg, decision, tools, used)
            res["usage"] = usage
            for k in ("cost_usd", "seconds", "turns"):
                totals[k] += usage.get(k, 0) or 0
            results.append(res)
            flag = " [GUARDRAIL: " + "; ".join(res["guardrail_notes"]) + "]" if res["guardrail_notes"] else ""
            print(f"[{res['id']}] {res['urgency']:<9} {','.join(res['actions'])} -> {','.join(res['routed_to']) or '-'}"
                  f"{flag}")
            if args.text or args.verbose:
                print(json.dumps({k: res[k] for k in ("patient_id", "intents", "tool_calls", "reply_draft")},
                                 indent=2, ensure_ascii=False))
        state = await tools.harness("get_run_state")

    out = Path(args.out or ROOT / "results" / f"{args.engine}_results.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    out.with_name(out.stem.replace("_results", "") + "_run_state.json").write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    totals["cost_usd"] = round(totals["cost_usd"], 4)
    totals["seconds"] = round(totals["seconds"], 1)
    print(f"\n{len(results)} messages -> {out}   totals: {totals}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--engine", choices=["claude", "rules"], default="claude")
    p.add_argument("--model", help="override Claude model id")
    p.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--messages", default=str(ROOT / "data" / "messages_sample.json"))
    p.add_argument("--only", help="comma-separated message ids")
    p.add_argument("--out", help="output path (default results/<engine>_results.json)")
    p.add_argument("--no-fallback", action="store_true", help="fail instead of falling back to rules")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--text", help="process a single ad-hoc message body")
    p.add_argument("--id", default="NEW001")
    p.add_argument("--from-name", default="")
    p.add_argument("--from-contact", default="")
    p.add_argument("--channel", default="sms")
    p.add_argument("--dob", help="stated date of birth for --text")
    load_dotenv()
    sys.exit(asyncio.run(main(p.parse_args())))
