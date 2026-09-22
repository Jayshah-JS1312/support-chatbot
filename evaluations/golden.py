"""The golden set: questions with the answer a good agent would give.

`behavioral.py` asks "did the agent DO the right thing" — the tool sequence, the
refusals, the store afterwards. Those are assertions: exact, cheap, and
blind to the one thing the customer actually reads. An agent can call every
tool correctly and still reply with a wrong date, or a confident invention.

So this file grades the ANSWER. Each row in golden.json is a question and a
reference answer written by hand from the order data and the policy docs —
the golden set. A row is scored on four numbers:

    facts       deterministic. Strings that must (or must not) appear.
    retrieval   deterministic. Did search_knowledge bring back the passage
                the row says the answer lives in?
    correct     judged. Does the reply convey the reference answer?
    grounded    judged. Is every claim in the reply supported by what the
                agent actually looked up?

The last two are an LLM judge — a second model call, temperature 0, given
the reference and the agent's own tool results, answering on a three-point
scale. That is deliberately the whole of it. RAGAS and friends wrap the
same idea in a framework; here the rubric is twelve lines you can read and
argue with, which is the point for a class. correct/grounded map onto what
RAGAS calls answer-correctness and faithfulness; retrieval is its
context-recall.

A judge you have not checked is just another opinion, so:

    python -m evaluations.golden --audit

It must score every reference 1.0, and must score an unrelated row's
reference LOWER. If it cannot do both, fix the rubric — or the reference —
before you trust a single row below it.

    python -m evaluations.golden
    python -m evaluations.golden --planner plan
    python -m evaluations.golden --only pol
    python -m evaluations.golden --runs 3

This suite includes input-policy cases and confirmation turns for actions
that change an order.
"""

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from evaluations import behavioral as evals
from support_chatbot import observe
from support_chatbot.llm import MODEL, chat

GOLDEN = Path(__file__).with_name("golden.json")
PASS_MARK = 1.0        # judged scores are 0 / 0.5 / 1; only 1 is a pass


# --------------------------------------------------------------------------
# 1. the golden set
# --------------------------------------------------------------------------
#
# store.py dates every order relative to today so the examples never go
# stale. The reference answers have to do the same, or the set rots in a
# week: {-4d} is four days ago, {+1d} tomorrow.

_DELTA = re.compile(r"\{([+-]\d+)d\}")


def _dates(text):
    return _DELTA.sub(
        lambda m: (date.today() + timedelta(days=int(m.group(1)))).isoformat(),
        text.replace("{today}", date.today().isoformat()))


def load(path=GOLDEN, only=""):
    rows = json.loads(Path(path).read_text())
    for row in rows:
        row["reference"] = _dates(row["reference"])
    return [r for r in rows if only.lower() in r["id"].lower()]


# --------------------------------------------------------------------------
# 2. the deterministic half
# --------------------------------------------------------------------------

def check_facts(row, reply):
    """Substring checks. Cheap, exact, and free — do these before judging."""
    low = reply.lower()
    misses = [f"reply lacks '{s}'" for s in row.get("must_include", [])
              if s.lower() not in low]
    misses += [f"reply contains '{s}'" for s in row.get("must_avoid", [])
               if s.lower() in low]
    checks = len(row.get("must_include", [])) + len(row.get("must_avoid", []))
    return (1.0 if not misses else 1 - len(misses) / checks), misses


def check_retrieval(row, observed):
    """Context recall: of the passages this row says hold the answer, how
    many did the agent's own search_knowledge call actually bring back?

    A wrong answer to a policy question has two very different causes —
    the retriever missed the passage, or the model read it and ignored it.
    One number tells them apart, so measure it separately.
    """
    want = row.get("sources")
    if not want:
        return None, []
    got = [(p.get("source", ""), p["policy"]) for o in observed
           if o["tool"] == "search_knowledge"
           for p in o["result"].get("passages", [])]
    # A row names either a file ("rules/authority.md") or a heading, whole
    # or by its document prefix ("Returns" matches every Returns section).
    # A LIST of those means "any one of these will do" — sometimes two
    # passages genuinely support the same answer, and demanding a
    # particular one measures the agent's phrasing, not its knowledge.
    missed = []
    for s in want:
        options = s if isinstance(s, list) else [s]
        if not any(src == o or head.startswith(o)
                   for o in options for src, head in got):
            missed.append(" or ".join(options))
    return 1 - len(missed) / len(want), [f"did not retrieve '{m}'" for m in missed]


# --------------------------------------------------------------------------
# 3. the judge
# --------------------------------------------------------------------------

JUDGE = """You grade replies from an Amazon customer-support agent.

THE CUSTOMER SAID
{turns}

THE REFERENCE ANSWER (what a correct reply must convey)
{reference}

WHAT THE AGENT LOOKED UP (its tool calls and the results it read)
{observed}

THE AGENT'S REPLY
{reply}

Give two scores, each exactly 0, 0.5 or 1.

correct — does the reply convey the reference answer?
  1    every key fact in the reference is there, and nothing contradicts it
  0.5  a key fact is missing or left vague, but nothing said is wrong
  0    it misses the point, or it contradicts the reference

grounded — is every SPECIFIC in the reply supported by the lookups above?
  1    every date, amount, order id, ticket, status and quoted policy rule
       traces back to a lookup
  0.5  one such specific is unsupported
  0    a specific is invented: an id, amount, date or rule in no lookup

Only specifics need a lookup. The agent's standing instructions — that it
handles Amazon orders, that card details must never be shared, that a human
agent can be brought in — are given to it, so they count as supported.
Offers, questions and suggested next steps are not claims and never lower
grounded. If the agent looked nothing up and asserts no specifics, it is 1.

Judge substance only. Wording, length, tone and politeness do not count. A
reply that covers the reference and adds more true detail still scores 1.
Dates may be written in any format.

Reply with JSON only:
{{"correct": 0, "correct_why": "<10 words>", "grounded": 0, "grounded_why": "<10 words>"}}"""


def judge(row, reply, observed, model):
    """One call, two scores. Returns the parsed verdict and what it cost."""
    prompt = JUDGE.format(
        turns="\n".join(row["turns"]),
        reference=row["reference"],
        observed=json.dumps(observed, indent=1)[:6000] or "(nothing)",
        reply=reply or "(no reply)")

    seq0 = observe.SEQ
    raw = chat([{"role": "user", "content": prompt}], model=model, temperature=0)
    cost = sum(e.get("cost") or 0 for e in observe.EVENTS
               if e.get("seq", 0) > seq0 and e["kind"] == "llm")

    verdict = _json(raw)
    for key in ("correct", "grounded"):
        try:
            verdict[key] = min(1.0, max(0.0, float(verdict.get(key, 0))))
        except (TypeError, ValueError):
            verdict[key] = 0.0
            verdict[key + "_why"] = f"judge returned no score: {raw[:80]}"
    return verdict, cost


def _json(text):
    """The model was told to return JSON. Sometimes it fences it anyway."""
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(match.group(0))
    except (AttributeError, json.JSONDecodeError):
        return {}


# --------------------------------------------------------------------------
# 4. one row, end to end
# --------------------------------------------------------------------------

def run_row(row, planner_name, judge_model):
    """Run the agent on the row, then score the reply four ways."""
    r = evals.run_case(row, planner_name)        # same runner as evals.py
    if r["error"]:
        return {**r, "facts": 0.0, "retrieval": None, "correct": 0.0,
                "grounded": 0.0, "score": 0.0, "judge_cost": 0,
                "fails": [f"crashed: {r['error']}"]}

    facts, fact_fails = check_facts(row, r["reply"])
    recall, recall_fails = check_retrieval(row, r["observed"])
    verdict, judge_cost = judge(row, r["reply"], r["observed"], judge_model)

    fails = fact_fails + recall_fails
    for key in ("correct", "grounded"):
        if verdict[key] < PASS_MARK:
            fails.append(f"{key} {verdict[key]}: {verdict.get(key + '_why', '')}")

    scored = [facts, verdict["correct"], verdict["grounded"]]
    if recall is not None:
        scored.append(recall)
    return {**r, "facts": facts, "retrieval": recall,
            "correct": verdict["correct"], "grounded": verdict["grounded"],
            "correct_why": verdict.get("correct_why", ""),
            "grounded_why": verdict.get("grounded_why", ""),
            "score": sum(scored) / len(scored), "judge_cost": judge_cost,
            "fails": fails}


# --------------------------------------------------------------------------
# 5. auditing the judge
# --------------------------------------------------------------------------

def audit(rows, judge_model):
    """Grade the reference answers themselves, before believing any score.

    Two controls per row. Own: the row's own reference, fed back as if the
    agent had said it — the judge must score that 1. Decoy: the reference
    from a row about something else — the judge must score it LOWER.

    Lower, not zero. In a support domain the references overlap honestly:
    two rows about the same order both mention the delivery date, half the
    rows offer a human agent. A judge that gives an off-topic answer 0.5 for
    the part it does happen to cover is being reasonable. What must never
    happen is a decoy scoring as high as the real answer — a judge that
    cannot separate those has no opinion, and every number it produces in
    the table above is noise.
    """
    print(f"judge audit · {len(rows)} rows × 2 controls · {judge_model}\n")
    own_scores, decoy_scores, apart, cost = [], [], [], 0.0
    for i, row in enumerate(rows):
        verdict, c = judge(row, row["reference"], [], judge_model)
        cost += c
        own = verdict["correct"]
        own_scores.append(own)

        decoy, other = _decoy(rows, i), None
        if decoy:
            v, c = judge(row, decoy, [], judge_model)
            cost += c
            other = v["correct"]
            decoy_scores.append(other)
            apart.append(other < own)

        ok = own == 1.0 and (other is None or other < own)
        print(f" {'  ' if ok else '<-'} {row['id']:<22} own={own:<4} "
              f"decoy={'-' if other is None else other:<4}  "
              f"{verdict.get('correct_why', '')[:42]}")

    positive = sum(own_scores) / len(own_scores)
    negative = sum(decoy_scores) / len(decoy_scores) if decoy_scores else 0.0
    print(f"\n own references scored      {positive:.2f}  (want 1.00)")
    print(f" unrelated references       {negative:.2f}  (want lower)")
    print(f" told apart                 {sum(apart)}/{len(apart)} rows  (want all)")
    print(f" judge cost ${cost:.3f}")
    return positive >= 0.9 and all(apart)


def _decoy(rows, i):
    """A reference from a row about something ELSE — the nearest row with no
    tag in common. Neighbouring rows are often about the same order, and a
    judge that gives one of those 0.5 for a near miss is being reasonable,
    not broken. A negative control has to be unambiguous to mean anything.
    """
    tags = set(rows[i].get("tags", []))
    for other in rows[i + 1:] + rows[:i]:
        if not tags & set(other.get("tags", [])):
            return other["reference"]
    return None


# --------------------------------------------------------------------------
# 6. the report
# --------------------------------------------------------------------------

def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _col(value):
    return "   - " if value is None else f"{value:5.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--planner", default="react", choices=["react", "plan"])
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--only", default="")
    ap.add_argument("--judge-model", default=MODEL)
    ap.add_argument("--audit", action="store_true",
                    help="grade the reference answers, not the agent")
    ap.add_argument("--out", default="artifacts/evaluations/golden.json")
    a = ap.parse_args()

    rows = load(only=a.only)
    if not rows:
        sys.exit(f"no rows match --only {a.only!r}")

    if a.audit:
        sys.exit(0 if audit(rows, a.judge_model) else 1)

    print(f"{len(rows)} rows × {a.runs} run(s) · planner={a.planner} · "
          f"agent={MODEL} · judge={a.judge_model}\n")
    print(f" {'row':<22} {'facts':>5} {'retr':>5} {'corr':>5} {'grnd':>5} "
          f"{'score':>6}  result")

    results = []
    for row in rows:
        outs = [run_row(row, a.planner, a.judge_model) for _ in range(a.runs)]
        passed = sum(not o["fails"] for o in outs)
        mark = "PASS" if passed == a.runs else ("FLAKY" if passed else "FAIL")

        print(f" {row['id']:<22} {_col(_mean([o['facts'] for o in outs]))} "
              f"{_col(_mean([o['retrieval'] for o in outs]))} "
              f"{_col(_mean([o['correct'] for o in outs]))} "
              f"{_col(_mean([o['grounded'] for o in outs]))} "
              f"{_mean([o['score'] for o in outs]):6.2f}  {mark}")
        for f in dict.fromkeys(f for o in outs for f in o["fails"]):
            print(f"   - {f}")
        results.append({"row": row["id"], "planner": a.planner,
                        "passed": passed, "runs": a.runs, "outcomes": outs})

    flat = [o for r in results for o in r["outcomes"]]
    passed = sum(r["passed"] for r in results)
    total = len(rows) * a.runs
    agent_cost = sum(o["cost"] for o in flat)
    judge_cost = sum(o["judge_cost"] for o in flat)

    print(f"\n {'':<22} {_col(_mean([o['facts'] for o in flat]))} "
          f"{_col(_mean([o['retrieval'] for o in flat]))} "
          f"{_col(_mean([o['correct'] for o in flat]))} "
          f"{_col(_mean([o['grounded'] for o in flat]))} "
          f"{_mean([o['score'] for o in flat]):6.2f}  average")
    print(f"\n{passed}/{total} rows clean ({100 * passed // total}%)  ·  "
          f"agent ${agent_cost:.3f} + judge ${judge_cost:.3f} = "
          f"${agent_cost + judge_cost:.3f}  ·  "
          f"{sum(o['ms'] for o in flat) / 1000:.0f}s")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump(results, open(a.out, "w"), indent=1)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
