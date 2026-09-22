"""Amazon support agent — profile, memory, planning, action, policy, RAG.

    support-chatbot                  ReAct planner, with the trace
    support-chatbot --plan           plan-and-execute planner
    support-chatbot --quiet          trace hidden

Type 'memory' at the prompt to dump what the agent currently holds.

Every turn passes through the policy layer. Long-term memory is read before
the turn and written afterward.
"""

import sys
import uuid

from support_chatbot import agent_profile as profile
from support_chatbot import plan_execute
from support_chatbot import planner
from support_chatbot import policy
from support_chatbot.llm import MODEL
from support_chatbot.memory import ConversationMemory, LongTermMemory, WorkingMemory


def main():
    argv = sys.argv[1:]
    use_plan = "--plan" in argv
    trace = "--quiet" not in argv

    system = profile.system_prompt()
    if use_plan:
        system += plan_execute.PLANNING_RULES
    else:
        system += planner.PLANNING_RULES

    convo = ConversationMemory(system)     # what was said
    work = WorkingMemory()                 # what is known and done
    work.session_id = "cli-" + uuid.uuid4().hex[:8]
    longterm = LongTermMemory()            # what we know about this customer
    mode = "plan-execute" if use_plan else "ReAct"

    print(f"[{profile.NAME} · {MODEL} · {mode}]  (ctrl-c or 'quit' to exit)\n")
    print(f"{profile.NAME}: {profile.GREETING}\n")

    while True:
        try:
            user = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user:
            continue
        if user.lower() in {"quit", "exit"}:
            break

        if user.lower() == "memory":
            print(f"\n-- conversation memory: {len(convo)} messages")
            print("-- working memory:")
            print(work.brief() or "   (empty)")
            print("-- long-term memory:")
            print(longterm.recall(work.customer_email, work.session_id)
                  or "   (not a returning customer)")
            print()
            continue

        # policy: input
        text, note = policy.check_input(user)
        work.turn += 1
        convo.add_user(text)
        print()

        if use_plan:
            reply = plan_execute.plan_execute(convo, work, trace=trace,
                                              longterm=longterm, extra=note)
        else:
            reply = planner.react(convo, work, trace=trace,
                                  longterm=longterm, extra=note)

        # policy: output, then remember the customer
        reply = policy.check_output(
            reply, work, text,
            context=longterm.recall(work.customer_email, work.session_id) or "")
        longterm.remember(work, session_id=work.session_id)

        print(f"{profile.NAME}: {reply}\n")


if __name__ == "__main__":
    main()
