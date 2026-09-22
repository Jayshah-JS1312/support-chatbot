"""The second planner: PLAN-AND-EXECUTE.

ReAct decides one step at a time — think, act, look, think again. Each
look costs a full model call with the whole transcript attached.

Plan-and-execute makes two calls, no matter how many tools run:

    1. PLAN     the model writes the entire list of tool calls up front
    2. EXECUTE  plain Python runs them in order — no model in the loop
    3. ANSWER   one final call turns the observations into a reply

Cheaper and faster when the path is predictable. Worse when it is not:
the planner cannot know an order number it has not looked up yet, so a
step that depends on an earlier result has to be left blank ("?") and the
whole thing re-planned once the blank can be filled. That re-plan is the
honest cost of planning ahead, and the trace shows it.
"""

import json

from support_chatbot import policy
from support_chatbot import tools
from support_chatbot.llm import complete
from support_chatbot.planner import notes

MAX_PLANS = 3      # plan -> execute -> re-plan, at most this many rounds

PLANNING_RULES = """
HOW YOU PLAN
You plan the whole task before acting, then act, then answer.
- When asked to plan, list every tool call needed, in order. Give the
  reason for each. If a later step needs a value you do not have yet
  (an order number you have not looked up), write "?" for that argument;
  you will be asked to re-plan once it is known.
- Plan the fewest steps that finish the job. Do not plan lookups you
  do not need.
- When asked to answer, use only what the observations say.
"""

PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_plan",
        "description": "Submit the list of tool calls to run, in order. "
                       "Submit an empty list if no tools are needed.",
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "What the customer wants, in one line"},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string",
                                     "enum": [s["function"]["name"] for s in tools.SCHEMAS]},
                            "args": {"type": "object",
                                     "description": "Arguments for the tool; use '?' for a value not yet known"},
                            "why": {"type": "string"},
                        },
                        "required": ["tool", "args", "why"],
                    },
                },
            },
            "required": ["goal", "steps"],
        },
    },
}

_TOOL_MENU = "\n".join(
    f"- {s['function']['name']}({', '.join(s['function']['parameters']['properties'])}): "
    f"{s['function']['description']}" for s in tools.SCHEMAS)


def _plan(convo, work, longterm, extra, observations):
    """Ask for a plan. Forced tool call, so the shape is guaranteed."""
    note = notes(work, longterm, extra) or ""
    if observations:
        note += "\n\nOBSERVATIONS SO FAR (fill in any '?' from these):\n" + \
                "\n".join(f"- {o['tool']}({json.dumps(o['args'])}) -> {json.dumps(o['result'])}"
                          for o in observations)
    note += "\n\nTOOLS AVAILABLE:\n" + _TOOL_MENU + \
            "\n\nNow call submit_plan with the steps to run."
    resp = complete(convo.messages(extra_system=note), tools=[PLAN_TOOL])
    msg = resp.choices[0].message
    if not msg.tool_calls:                       # it answered instead of planning
        return None, msg.content
    try:
        return json.loads(msg.tool_calls[0].function.arguments), None
    except json.JSONDecodeError:
        return {"goal": "?", "steps": []}, None


def _unknown(args):
    return any(v in ("?", "", None) for v in args.values())


def plan_execute(convo, work, trace=True, steps=None, longterm=None, extra=None):
    """Same signature as planner.react, so the two are interchangeable."""
    observations = []
    n = 0

    for round_no in range(1, MAX_PLANS + 1):
        plan, direct = _plan(convo, work, longterm, extra, observations)
        if direct is not None:                  # no plan needed; model just answered
            convo.add_assistant({"role": "assistant", "content": direct})
            return direct

        if trace:
            print(f"  [plan {round_no}] goal: {plan.get('goal')}")
            for i, st in enumerate(plan.get("steps", []), 1):
                print(f"      {i}. {st['tool']}({json.dumps(st['args'])})  — {st['why']}")

        # EXECUTE — plain Python, no model. Stop at the first blank argument
        # or the first refusal; either one needs a fresh plan.
        replan = False
        for st in plan.get("steps", []):
            name, args = st["tool"], dict(st.get("args") or {})
            if _unknown(args):
                replan = True
                break
            n += 1
            result = policy.guarded_run(name, args, work)
            work.record(name, args, result)
            observations.append({"tool": name, "args": args, "result": result})
            if steps is not None:
                steps.append({"n": n, "thought": st["why"], "tool": name,
                              "args": args, "observation": result})
            if trace:
                print(f"      -> {name}: {json.dumps(result)[:110]}")
            if "error" in result:
                replan = False                    # a refusal ends the plan; answer now
                break
            # a confirmation preview is not a failure: keep running the
            # steps that do not depend on it
        if not replan:
            break

    # ANSWER — one call, tools off, observations in.
    summary = "\n".join(f"- {o['tool']}({json.dumps(o['args'])}) -> {json.dumps(o['result'])}"
                        for o in observations) or "(no tools were run)"
    note = (notes(work, longterm, extra) or "") + \
           "\n\nWHAT YOU DID THIS TURN:\n" + summary + \
           "\n\nNow write the reply to the customer."
    resp = complete(convo.messages(extra_system=note))
    reply = resp.choices[0].message.content
    # Keep the transcript honest: record the reply, not the plan machinery.
    convo.add_assistant({"role": "assistant", "content": reply})
    return reply
