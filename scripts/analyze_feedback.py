"""Feedback analysis: integrate human corrections into golden.json and policy.

Reads state/feedback.jsonl (human feedback on agent responses), analyzes patterns,
categorizes corrections, and proposes golden.json reweighting.

    python -m scripts.analyze_feedback
    python -m scripts.analyze_feedback --propose
    python -m scripts.analyze_feedback --apply

Feedback format (JSONL):
    {
        "golden_id": "ord-status",           # which golden row was it
        "correction": "...",                 # what should the agent have said
        "category": "tone|incomplete_info|policy_violation|factual_error|knowledge_gap",
        "issue": "...",                      # description of the problem
        "timestamp": "2026-09-13T...",
        "feedbacker": "human"
    }

Output:
    state/feedback_summary.json             patterns, recommendations, before/after scores
"""

import argparse
import json
import sys
from pathlib import Path
from collections import defaultdict, Counter
from datetime import datetime

from support_chatbot import PROJECT_ROOT, STATE_DIR


def load_feedback(feedback_path):
    """Load feedback entries from JSONL."""
    if not feedback_path.exists():
        return []

    entries = []
    with open(feedback_path) as f:
        for line in f:
            if line.strip():
                try:
                    entry = json.loads(line)
                    # Normalize browser submissions and manually curated feedback.
                    entry.setdefault("golden_id", entry.get("golden_row_id") or "unknown")
                    entry.setdefault("correction", entry.get("corrected_response", ""))
                    entry.setdefault("issue", entry.get("reason", ""))
                    entry.setdefault("category", "uncategorized")
                    entries.append(entry)
                except json.JSONDecodeError:
                    print(f"Warning: skipped malformed JSON: {line}", file=sys.stderr)
    return entries


def load_golden(golden_path):
    """Load golden.json."""
    with open(golden_path) as f:
        return json.load(f)


def analyze_feedback(entries):
    """Analyze feedback patterns."""
    if not entries:
        return {
            "total_feedback": 0,
            "by_category": {},
            "by_golden_id": {},
            "patterns": [],
            "top_issues": []
        }

    analysis = {
        "total_feedback": len(entries),
        "by_category": Counter(),
        "by_golden_id": defaultdict(list),
        "patterns": [],
        "top_issues": Counter()
    }

    # Categorize feedback
    for entry in entries:
        cat = entry.get("category", "uncategorized")
        analysis["by_category"][cat] += 1

        golden_id = entry.get("golden_id", "unknown")
        analysis["by_golden_id"][golden_id].append(entry)

        if "issue" in entry:
            analysis["top_issues"][entry["issue"]] += 1

    # Extract patterns
    patterns = []
    for cat, count in sorted(analysis["by_category"].items(), key=lambda x: -x[1]):
        if count >= 1:
            patterns.append({
                "category": cat,
                "count": count,
                "percentage": round(100 * count / len(entries), 1)
            })

    analysis["patterns"] = patterns

    # Top issues
    analysis["top_issues"] = [
        {"issue": issue, "count": count}
        for issue, count in analysis["top_issues"].most_common(5)
    ]

    return analysis


def propose_reweighting(golden, feedback_by_id):
    """Propose golden.json changes based on feedback."""
    if not feedback_by_id:
        return []

    proposals = []

    for golden_id, feedback_list in feedback_by_id.items():
        golden_row = next((r for r in golden if r.get("id") == golden_id), None)
        if not golden_row:
            continue

        # Count feedback by category
        categories = Counter(f.get("category") for f in feedback_list)

        # Create proposal
        proposal = {
            "golden_id": golden_id,
            "current_tags": golden_row.get("tags", []),
            "feedback_count": len(feedback_list),
            "feedback_categories": dict(categories),
            "recommendation": ""
        }

        # Determine recommendation
        if categories.get("policy_violation", 0) >= 2:
            if "policy" not in proposal["current_tags"]:
                proposal["new_tags"] = proposal["current_tags"] + ["policy"]
                proposal["recommendation"] = "Add 'policy' tag — multiple policy violations detected"

        if categories.get("factual_error", 0) >= 2:
            if "knowledge_gap" not in proposal["current_tags"]:
                proposal["new_tags"] = proposal["current_tags"] + ["knowledge_gap"]
                proposal["recommendation"] = "Add 'knowledge_gap' tag — multiple factual errors detected"

        if categories.get("tone", 0) >= 2:
            if "tone" not in proposal["current_tags"]:
                proposal["new_tags"] = proposal["current_tags"] + ["tone"]
                proposal["recommendation"] = "Add 'tone' tag — multiple tone issues detected"

        if not proposal["recommendation"]:
            proposal["recommendation"] = "Monitor — issues detected but thresholds not met for changes"

        proposals.append(proposal)

    return proposals


def generate_summary(entries, golden, feedback_path):
    """Generate comprehensive feedback summary."""
    analysis = analyze_feedback(entries)
    proposals = propose_reweighting(golden, analysis["by_golden_id"])

    summary = {
        "timestamp": datetime.now().isoformat(),
        "feedback_count": len(entries),
        "source": str(feedback_path),
        "analysis": {
            "total": analysis["total_feedback"],
            "by_category": dict(analysis["by_category"]),
            "top_patterns": analysis["patterns"],
            "top_issues": analysis["top_issues"]
        },
        "golden_rows_with_feedback": len(analysis["by_golden_id"]),
        "proposals": proposals,
        "next_steps": generate_next_steps(analysis, proposals)
    }

    return summary


def generate_next_steps(analysis, proposals):
    """Suggest next actions based on analysis."""
    steps = []

    if analysis["total_feedback"] < 3:
        steps.append({
            "action": "collect_more_feedback",
            "reason": f"Only {analysis['total_feedback']} feedback entries; aim for at least 3-5 per category"
        })

    if proposals:
        steps.append({
            "action": "review_proposals",
            "reason": f"{len(proposals)} golden rows have actionable feedback"
        })

    # Category-specific guidance
    cat_counts = analysis["by_category"]
    if cat_counts.get("policy_violation", 0) >= 2:
        steps.append({
            "action": "review_policy_layer",
            "reason": f"{cat_counts['policy_violation']} policy violations — check policy.py guardrails"
        })

    if cat_counts.get("knowledge_gap", 0) >= 2:
        steps.append({
            "action": "review_knowledge_base",
            "reason": f"{cat_counts['knowledge_gap']} knowledge gaps — update knowledge/ documents"
        })

    return steps


def apply_proposals(golden_path, proposals):
    """Apply proposals to golden.json."""
    with open(golden_path) as f:
        golden = json.load(f)

    changes_made = 0
    for proposal in proposals:
        if "new_tags" not in proposal:
            continue

        row = next((r for r in golden if r.get("id") == proposal["golden_id"]), None)
        if row:
            old_tags = row.get("tags", [])
            new_tags = proposal["new_tags"]
            if old_tags != new_tags:
                row["tags"] = new_tags
                print(f"  {proposal['golden_id']}: {old_tags} → {new_tags}")
                changes_made += 1

    if changes_made > 0:
        backup_path = golden_path.with_suffix('.json.bak')
        golden_path.rename(backup_path)
        with open(golden_path, 'w') as f:
            json.dump(golden, f, indent=1)
        print(f"\nApplied {changes_made} changes. Backup: {backup_path.name}")
        return True
    else:
        print("No changes to apply.")
        return False


def main():
    ap = argparse.ArgumentParser(
        description="Analyze human feedback and integrate into golden.json"
    )
    ap.add_argument("--propose", action="store_true",
                    help="Show proposed changes without applying")
    ap.add_argument("--apply", action="store_true",
                    help="Apply proposed changes to golden.json")
    ap.add_argument("--out", default="state/feedback_summary.json",
                    help="Where to write the summary")
    a = ap.parse_args()

    feedback_path = STATE_DIR / "feedback.jsonl"
    golden_path = PROJECT_ROOT / "evaluations" / "golden.json"

    # Load data
    entries = load_feedback(feedback_path)
    golden = load_golden(golden_path)

    if not entries:
        print(f"No feedback found at {feedback_path}")
        print("Submit feedback in the web UI, then run this command again.")

        # Create empty summary
        summary = {
            "timestamp": datetime.now().isoformat(),
            "feedback_count": 0,
            "status": "waiting_for_feedback",
            "message": "No feedback entries yet. Run this again after Bravo captures feedback."
        }
        out_path = PROJECT_ROOT / a.out
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, 'w') as f:
            json.dump(summary, f, indent=2)
        print(f"Summary written to {out_path}")
        return 0

    # Generate summary
    summary = generate_summary(entries, golden, feedback_path)

    # Show summary
    print(f"\n=== Feedback Analysis ===")
    print(f"Total feedback entries: {summary['feedback_count']}")
    print(f"\nTop patterns:")
    for pattern in summary["analysis"]["top_patterns"]:
        print(f"  {pattern['category']:20} {pattern['count']:3} ({pattern['percentage']:5.1f}%)")

    print(f"\nTop issues:")
    for issue in summary["analysis"]["top_issues"][:3]:
        print(f"  {issue['count']:2}× {issue['issue']}")

    # Show proposals if any
    if summary["proposals"]:
        print(f"\nProposals ({len(summary['proposals'])} golden rows):")
        for prop in summary["proposals"]:
            print(f"  {prop['golden_id']:20} {prop['recommendation']}")

    # Write summary
    out_path = PROJECT_ROOT / a.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(summary, f, indent=2)
    print(f"\nSummary written to {out_path}")

    # Handle --propose and --apply
    if a.propose:
        print("\n=== Proposed Changes ===")
        for prop in summary["proposals"]:
            if "new_tags" in prop:
                print(f"{prop['golden_id']}: {prop['recommendation']}")
                print(f"  Current tags: {prop['current_tags']}")
                print(f"  New tags:     {prop['new_tags']}")

    if a.apply:
        print("\n=== Applying Changes ===")
        apply_proposals(golden_path, summary["proposals"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
