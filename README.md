# Maplewood front-desk agent (Edvak Forge take-home)

An AI agent that works a clinic inbox. It identifies and verifies the sender, takes action through an MCP tool server (refills, scheduling, routing), drafts a reply, and escalates anything that might be an emergency. An evaluation harness scores it against the labels.

## Run it

```bash
python -m venv .venv && .venv/Scripts/activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
echo ANTHROPIC_API_KEY=sk-ant-... > .env               # or export it

python run.py                                          # Claude agent, 40 sample messages -> results/claude_results.json
python eval.py results/claude_results.json             # -> results/claude_eval.md

python run.py --messages data/messages_holdout.json --out results/claude_holdout_results.json
python eval.py results/claude_holdout_results.json --labels data/labels_holdout.json

python run.py --engine rules                           # offline baseline, no API key needed
python -m unittest discover -s tests -t .              # 18 tests, no API key needed

# a message nobody has seen (for the walkthrough)
python run.py --text "My son swallowed some of my pills" --from-name "Anna Rossi" --from-contact 555-0109
```

`run.py` options include `--only M003,M017`, `--effort low|medium|high|xhigh|max` (default `high`), `--model`, `--no-fallback` and `-v`.

## Architecture

```
run.py ──> Engine (Claude tool loop | rules baseline)
             │  MCP stdio
             ▼
        clinic/server.py (FastMCP, 17 tools) ──> clinic/store.py (data + policy, in-memory state)
             │
        agent/session.py finalize(): guardrail floor + read back what actually happened
             ▼
        results/*.json ──> eval.py ──> results/*_eval.md
```

**1. Policy lives in the tools, not the prompt.** `clinic/store.py` is plain Python and covered by unit tests. The rules the clinic can't afford to get wrong are enforced on the server, so a confused or manipulated model can't break them:

| Rule | Enforced by |
|---|---|
| No PHI without verification | Every patient-data and appointment tool requires a `verification_id`, which only `verify_sender` issues (name + DOB match, or the contact on file; caregivers through their contact on file). |
| Agent never approves a prescription | No such tool exists. `prepare_refill_request` re-runs the policy check and refuses controlled substances, patients not seen in 12 months, zero refills and early requests. |
| Emergency replies carry no clinical content | `escalate_emergency` pages `clinician_urgent` and locks the reply to the policy template (English/Spanish). `draft_reply` can't overwrite it. |
| Booking rules | `book_appointment` and `reschedule_appointment` reject bookings on holidays, during leave, at lunch, off the :00/:20/:40 grid or overlapping another booking, and say why. `find_free_slots` only returns valid slots. |

Tools return reasons and next steps (`"status": "too_early", "earliest_request_date": "2026-11-17"`), so the model follows policy by reading results instead of recalling rules.

**2. The LLM does the reading and judgement.** It decides intent (including several intents in one message), what the patient means by "Friday afternoon", whether "I'm dying to get in" is an idiom, and how to word the reply. It's a manual tool-use loop on `claude-opus-5-5` with adaptive thinking (effort `high`), prompt caching, and server-side refusal fallback (`fallbacks: "default"`). It ends with a strict-schema `submit_triage` call. The output fields that describe side effects (`routed_to`, `reply_draft`, `tool_calls`) are read back from the server, not taken from the model's own account.

**3. A deterministic guardrail sets a floor** (`agent/guardrails.py`). High-precision red-flag patterns can only raise urgency, never lower it. If the model under-triages, the harness escalates on its behalf. It also routes buried mood concerns to `clinician_urgent` and flags injection text. Every intervention is recorded in `guardrail_notes` and counted by the eval, so you can see how often the model needed rescuing.

**4. A rules engine serves as the baseline and fallback** (`agent/rules_agent.py`). It uses the same tools with keyword intents. If the Claude call fails for a message (outage, refusal, loop limit), that message falls back to rules and is marked `degraded`, so no message is dropped.

**Why MCP over stdio and in-memory state:** it's the simplest setup that meets "persist for the length of a run". One server process handles a whole run, so a slot booked by M010 is no longer free when M024 runs. Swapping in MongoDB would only change `store.py`.

## What I reused and what I built

- **Reused:** the Anthropic Python SDK, the MCP Python SDK (FastMCP server and stdio client; pinned below 2.x because 2.x renamed the APIs), and the Python standard library. No agent framework: the loop is about 60 lines, and owning it makes tool calls easy to log and errors easy to handle.
- **Built:** the domain/policy layer, the MCP tool surface, the agent loop and prompt, the guardrail, the rules baseline, the evaluation harness (including must-not checkers and whole-state invariants), unit tests, and a 16-message held-out set with labels (`data/*_holdout.json`).
- **AI assistance:** written with the help of an AI coding assistant.

## Results

### Rules baseline (deterministic, $0)

| | Sample (40) | Held-out (16) |
|---|---|---|
| Urgency accuracy | 100% | 81.2% |
| **Emergency recall** | **5/5** | **0/3** |
| Action exact match | 95.0% | 43.8% |
| Action micro F1 | 0.968 | 0.55 |
| patient_id accuracy | 100% | 93.8% |
| must_not violations | 0 | 3 |
| Policy invariants on final state | none violated | none violated |

**Don't trust the sample number.** I wrote the baseline after reading the 40 messages, so 95% shows overfitting, not skill. The held-out set is the honest measure, and there the baseline misses all three emergencies:

- H01 stroke signs in lay terms ("face looks droopy", "words coming out wrong")
- H11 chest pain written in Spanish
- H14 stimulant overdose with near-fainting

None of them matched an English keyword list. That's why keywords are only the floor and the LLM does the judgement. I deliberately did **not** add these phrasings to the guardrail after seeing them, because that would contaminate the held-out set.

Other baseline errors: on H07 it prepared an insulin refill nobody asked for, because the injection path assumed a refill. On H04 it booked an appointment when the patient was asking about one.

### Claude agent

**Not run yet:** no API key was available while building this. To produce `results/claude_eval.md` and `results/claude_holdout_eval.md`, run the four commands in "Run it". The eval reports guardrail interventions and fallback counts next to the accuracy numbers, so a run that only looked good because the floor caught things will show it.

What is verified without a key (`tests/test_claude_loop.py`): a scripted fake client drives the real MCP server through the Claude engine's loop. It checks the request shape (model, adaptive thinking, effort, strict `submit_triage`, no forced `tool_choice`), that every `tool_use` gets its `tool_result`, that side effects are read back from the server, that the guardrail escalates when the model under-triages a chest-pain message, and that a refusal raises so `run.py` falls back to rules. Running `run.py` with an unreachable API endpoint also confirms the fallback: every message degrades to rules, is marked `degraded`, and emergencies are still escalated.

**Cost estimate:** about 6 turns per message, roughly 7k cached prompt plus 1–4k fresh tokens per turn, and about 0.5–1k output tokens per turn including thinking. At $4/$20 per million tokens (cache reads $0.20), that's roughly $0.10–0.25 per message, or **$5–10 for all 56 messages**. Each result's `usage` field records the measured cost, and `eval.py` totals it. `--effort medium` should roughly halve it; measure before switching.

### Disagreements with the labels

The eval scores against the labels as given. These are the cases where I think the agent's choice is defensible:

- **M022** ("cancel my appointment tomorrow"): Hannah Lee has *two* appointments tomorrow (KIM 11:00 and OKAFOR 15:00 telehealth). The label expects `cancel_appointment`. My agents ask which one rather than risk cancelling the wrong one.
- **M004 vs M026**: both are refills for a patient not seen in 12 months. The policy says "offer an appointment *and route to the provider*", but only M026's label includes `route_to_provider`. I follow the policy for both, so M004 shows an extra action.
- **`patient_id`**: the brief says null for an unverified sender, but the labels give the patient the message is about (M011, M027). I follow the labels and add a separate `sender_verified` field.

## What it gets wrong, or could

- Keyword red flags are English-only and phrase-specific (see the held-out results). The LLM is the real emergency detector, and its miss rate on new phrasings is the number that matters most.
- The must-not checkers for `automated_clinical_advice` and `disclose_phi` are regex heuristics over the reply text. They catch obvious failures, but a human should read the replies. An LLM-judge pass would be the next step.
- If Claude fails partway through a message, tools it already called (a booking, say) stay in place before the rules fallback runs, which could duplicate an action.
- The rules engine's Spanish is phrase substitution and can produce mixed-language replies.
- The run uses a fixed clock (9 Nov), and `find_free_slots` doesn't exclude the patient's own conflicting appointments. `book_appointment` does reject them, so the model just retries.

## With another week

1. Run Claude at several effort levels, check how stable results are across repeated runs, and report the variance alongside the score.
2. Expand the held-out set to 100+ messages, written or reviewed by a clinician: emergencies in other languages and lay terms, caregivers, multi-intent messages. Then add an LLM judge for reply quality, clinical content and PHI leakage.
3. Add a parallel safety classifier: a cheap, separate model call per message that only answers "possible emergency?", with recall measured on its own.
4. Add idempotency keys on side-effecting tools so retries and fallbacks can't duplicate bookings.
5. Add a human-review UI: draft replies and proposed actions sit in a queue, and staff approve them with one click.

## Before this touched real patient data

- A HIPAA BAA with the LLM provider, a zero-data-retention configuration, PHI minimisation in prompts, and encryption at rest and in transit.
- Real identity verification. Matching on phone, email or name+DOB is weak (spoofed SMS, shared family phones). Use portal authentication or one-time codes, and record caregiver proxies with their scope.
- Human sign-off on every outgoing message and every booking change at first. Expand autonomy one category at a time, with measured error rates.
- Clinical governance: a clinician signs off on the emergency criteria and the reply template, keeps a 24/7 urgent queue with a paging SLA, and responds to messages that arrive after hours.
- Monitoring: an audit log of every tool call (the server already keeps one), alerting on escalations, drift checks on fresh labelled samples, and an incident process for missed emergencies.
- Integration with the real EHR and scheduling system through their APIs, with least-privilege service accounts instead of direct data access.
- Security review of prompt injection, rate limiting, and abuse handling for unknown senders.

## Layout

```
clinic/store.py        data, policy, in-memory state (unit-tested)
clinic/server.py       MCP server exposing the tools
agent/claude_agent.py  Claude tool loop + system prompt
agent/rules_agent.py   deterministic baseline / fallback
agent/guardrails.py    red-flag floor, action vocabulary
agent/session.py       MCP client, finalize() merge + guardrail
run.py / eval.py       CLI and evaluation harness
data/                  provided data + held-out set I wrote
results/               outputs, run state (audit log) and eval reports
tests/                 policy tests + offline test of the Claude loop
```
