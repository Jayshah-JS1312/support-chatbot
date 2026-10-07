"""Element 3 of the agent: PLANNING — the ReAct loop.

ReAct = Reason + Act. The agent works in a visible cycle:

    Thought:      what do I know, what do I need next, why this tool
    Action:       the tool call
    Observation:  what came back

...repeated until it has enough to answer. The planner makes the decision
trace explicit by requiring a `thought` argument on every tool request.

That one change buys three things:
  - you can read why the agent did what it did (debugging)
  - the model plans before it acts instead of after (better tool choice)
  - a bad plan is visible in the trace, not buried in a wrong answer

Every tool call goes through policy.guarded_run() instead of
tools.run(), and the note injected before each step (notes()) also
carries long-term memory and anything the input policy flagged.
"""

import json

from support_chatbot import policy
from support_chatbot import tools
from support_chatbot.llm import MODEL, complete

MAX_STEPS = 6

PLANNING_RULES = """
HOW YOU PLAN
Work one step at a time, and think before each step.
- Before every tool call, state your reasoning in the 'thought' argument:
  what you already know, what is still missing, and why this tool is next.
- Take ONE action at a time. Read the observation before deciding again.
- Errors come in three kinds, and they are handled differently:
  * "retry": true  -> YOU called the tool wrongly. Fix the arguments and
    call it again. Do not tell the customer about this.
  * "temporarily_unavailable": true -> the dependency failed. Do not guess
    or retry it in this turn. State what could not be verified and offer a
    later retry or human support.
  * no retry flag  -> a POLICY refusal. Never repeat the call. Tell the
    customer the rule and offer their next option.
- Stop as soon as you can answer. Do not call tools you do not need.
"""


def _schemas_with_thought():
    """Copy the tool schemas, adding a required 'thought' to each one."""
    out = []
    for schema in tools.SCHEMAS:
        fn = json.loads(json.dumps(schema["function"]))   # deep copy
        params = fn["parameters"]
        # 'thought' goes LAST, and that position was not a style choice.
        # It was first, which reads better — think, then act — and the
        # golden set said otherwise: the model filled in the thought and
        # stopped, emitting cancel_order({}) with no order_id at all. Over
        # 27 rows, 7 of 41 tool calls came back with empty arguments, each
        # one a wasted step and sometimes a wrong answer to the customer.
        # Moving one key to the end of the dict: 0 of 39.
        #
        # The model writes JSON left to right, so whatever comes first is
        # what it commits to. Put the reasoning first and the reasoning is
        # all you reliably get.
        params["properties"] = {
            **params["properties"],
            "thought": {
                "type": "string",
                "description": "Your reasoning: what you know, what you "
                               "still need, and why this tool is next.",
            },
        }
        params["required"] = params["required"] + ["thought"]
        out.append({"type": "function", "function": fn})
    return out


SCHEMAS = _schemas_with_thought()


def notes(work, longterm=None, extra=None):
    """Everything the model should know beyond the transcript, as one note."""
    parts = [work.brief()]
    if longterm is not None:
        parts.append(longterm.recall(work.customer_email,
                                     getattr(work, "session_id", None)))
    parts.append(extra)
    parts = [p for p in parts if p]
    return "\n\n".join(parts) if parts else None


def react(convo, work, trace=True, steps=None, longterm=None, extra=None):
    """Run the ReAct loop until the agent produces an answer for the customer.

    convo    : ConversationMemory — what was said, sent in full to the model
    work     : WorkingMemory      — what is known, injected as a short note
    longterm : LongTermMemory     — what we know about this customer already
    extra    : a one-off note (e.g. from the input policy check)
    trace    : print the Thought/Action/Observation trace to the terminal
    steps    : optional list; each step is appended as a dict so a caller
               (the web UI) can render the trace instead of printing it
    """
    for step in range(1, MAX_STEPS + 1):
        # Working memory is re-read before EVERY step, so the agent plans
        # against what it has already established, not just the transcript.
        response = complete(convo.messages(extra_system=notes(work, longterm, extra)),
                            tools=SCHEMAS)
        message = response.choices[0].message
        convo.add_assistant(message.model_dump(exclude_none=True))

        # No action requested -> the agent is done reasoning, this is the answer.
        if not message.tool_calls:
            return message.content

        for call in message.tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            # Pull the reasoning out; it is for us, not for the tool.
            thought = args.pop("thought", "(no thought given)")
            result = policy.guarded_run(name, args, work)   # policy first, then the tool
            work.record(name, args, result)      # <- the observation updates what we know

            if steps is not None:
                steps.append({"n": step, "thought": thought, "tool": name,
                              "args": args, "observation": result})

            if trace:
                arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
                print(f"  [{step}] Thought: {thought}")
                print(f"      Action: {name}({arg_str})")
                print(f"      Observation: {json.dumps(result)}\n")

            convo.add_observation(call.id, result)

    return ("I'm not able to sort this out myself. Let me get a human agent "
            "to take a look.")


CHAINS_OF_THOUGHT_RULES = """
HOW YOU PLAN
Break down your reasoning into clear steps before acting.
- Think through the problem step by step in your internal monologue
- Before every tool call, state your complete reasoning in the 'thought' argument:
  what you already know, what is still missing, and why this tool is next.
- Take ONE action at a time. Read the observation before deciding again.
- Errors come in three kinds, and they are handled differently:
  * "retry": true  -> YOU called the tool wrongly. Fix the arguments and
    call it again. Do not tell the customer about this.
  * "temporarily_unavailable": true -> the dependency failed. Do not guess
    or retry it in this turn. State what could not be verified and offer a
    later retry or human support.
  * no retry flag  -> a POLICY refusal. Never repeat the call. Tell the
    customer the rule and offer their next option.
- Stop as soon as you can answer. Do not call tools you do not need.
"""


def chains_of_thought(convo, work, trace=True, steps=None, longterm=None, extra=None):
    """Run the chains-of-thought loop until the agent produces an answer.

    Chains of thought: the agent reasons through the problem step-by-step
    before making tool calls, with explicit intermediate reasoning steps.

    convo    : ConversationMemory — what was said, sent in full to the model
    work     : WorkingMemory      — what is known, injected as a short note
    longterm : LongTermMemory     — what we know about this customer already
    extra    : a one-off note (e.g. from the input policy check)
    trace    : print the Thought/Action/Observation trace to the terminal
    steps    : optional list; each step is appended as a dict so a caller
               (the web UI) can render the trace instead of printing it
    """
    for step in range(1, MAX_STEPS + 1):
        # Working memory is re-read before EVERY step, so the agent plans
        # against what it has already established, not just the transcript.
        response = complete(convo.messages(extra_system=notes(work, longterm, extra)),
                            tools=SCHEMAS)
        message = response.choices[0].message
        convo.add_assistant(message.model_dump(exclude_none=True))

        # No action requested -> the agent is done reasoning, this is the answer.
        if not message.tool_calls:
            return message.content

        for call in message.tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            # Pull the reasoning out; it is for us, not for the tool.
            thought = args.pop("thought", "(no thought given)")
            result = policy.guarded_run(name, args, work)   # policy first, then the tool
            work.record(name, args, result)      # <- the observation updates what we know

            if steps is not None:
                steps.append({"n": step, "thought": thought, "tool": name,
                              "args": args, "observation": result})

            if trace:
                arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
                print(f"  [{step}] Thought: {thought}")
                print(f"      Action: {name}({arg_str})")
                print(f"      Observation: {json.dumps(result)}\n")

            convo.add_observation(call.id, result)

    return ("I'm not able to sort this out myself. Let me get a human agent "
            "to take a look.")
