#!/usr/bin/env python3
"""Measure embedding model latency as knowledge base scales.

This script answers: "At what knowledge base size does the embedding model break?"

It starts with 48 chunks (current state) and incrementally adds test chunks to
measure search latency at 48, 100, 500, 1000, and 5000 chunks. It identifies
the breakpoint where latency exceeds 500ms (typical SLA).

Output:
  - scripts/latency_results.csv: (num_chunks, latency_ms, timestamp)
  - scripts/latency_curve.png: graph of latency vs chunks
"""

import csv
import sys
import time
from pathlib import Path

from support_chatbot import KNOWLEDGE_DIR


def get_knowledge_module():
    """Import and reload the knowledge module."""
    if "support_chatbot.knowledge" in sys.modules:
        del sys.modules["support_chatbot.knowledge"]
    if "support_chatbot.embedder" in sys.modules:
        del sys.modules["support_chatbot.embedder"]

    from support_chatbot import knowledge

    # Force reload of collection cache
    knowledge._COLLECTION = None

    return knowledge


def count_chunks(knowledge):
    """Count chunks in the current knowledge base."""
    chunks = knowledge._chunks()
    return len(chunks)


def add_test_chunks(base_count, target_count):
    """Add temporary test chunks to reach target_count.

    Returns the list of created file paths (for cleanup).
    """
    knowledge = get_knowledge_module()
    current = count_chunks(knowledge)

    if current >= target_count:
        return []

    # Create test chunks
    num_to_add = target_count - current
    created_files = []

    # Add test chunks to policies directory (largest category)
    policies_dir = KNOWLEDGE_DIR / "policies"
    policies_dir.mkdir(parents=True, exist_ok=True)

    for i in range(num_to_add):
        test_file = policies_dir / f"_test_chunk_{i:05d}.md"
        test_content = f"""# Test Policy {i}

## Test Section {i}

This is a test chunk created for latency measurement. Chunk number {i}.
The quick brown fox jumps over the lazy dog. This is filler text to make
the chunk have some content. Latency measurement is important for scaling.
We are testing how the embedding model scales to 5000 chunks or more.
"""
        test_file.write_text(test_content)
        created_files.append(test_file)

    print(f"  Added {len(created_files)} test chunks (current: {current} -> target: {target_count})")

    return created_files


def cleanup_test_chunks(files):
    """Remove test chunk files."""
    for f in files:
        try:
            f.unlink()
        except Exception as e:
            print(f"  Warning: failed to remove {f}: {e}")


def measure_latency(knowledge, num_queries=10):
    """Measure search latency for num_queries searches.

    Returns average latency in milliseconds.
    """
    test_queries = [
        "What is your return policy?",
        "How can I track my order?",
        "Can I change my order?",
        "What about international shipping?",
        "How do I contact support?",
        "What payment methods do you accept?",
        "Is there a warranty?",
        "Can I cancel my order?",
        "What is your refund policy?",
        "Do you ship to my country?",
    ]

    latencies = []

    for q in test_queries[: min(num_queries, len(test_queries))]:
        # Measure the search latency
        start = time.perf_counter()
        results = knowledge.search(q, k=3)
        elapsed = (time.perf_counter() - start) * 1000  # milliseconds

        latencies.append(elapsed)

    avg_latency = sum(latencies) / len(latencies) if latencies else 0
    return avg_latency, latencies


def main():
    """Main measurement loop."""
    print("=" * 70)
    print("Embedding Model Latency Measurement")
    print("=" * 70)

    scales = [48, 100, 500, 1000, 5000]
    results = []

    try:
        for scale in scales:
            print(f"\nMeasuring at {scale} chunks...")

            # Add test chunks if needed
            created_files = add_test_chunks(48, scale)

            # Reload knowledge module and measure
            knowledge = get_knowledge_module()
            current = count_chunks(knowledge)
            print(f"  Current chunk count: {current}")

            avg_latency, latencies = measure_latency(knowledge, num_queries=5)

            result = {
                "num_chunks": current,
                "latency_ms": round(avg_latency, 2),
                "timestamp": time.time(),
                "min_ms": round(min(latencies), 2),
                "max_ms": round(max(latencies), 2),
            }

            results.append(result)

            print(
                f"  Avg latency: {avg_latency:.2f}ms (min: {min(latencies):.2f}, max: {max(latencies):.2f})"
            )

            if avg_latency > 500:
                print(f"  ⚠️  BREAKPOINT: Latency exceeds 500ms SLA at {current} chunks")

            # Cleanup test chunks
            cleanup_test_chunks(created_files)

    except Exception as e:
        print(f"Error during measurement: {e}")
        import traceback

        traceback.print_exc()
        return 1

    # Save results to CSV
    csv_file = Path(__file__).parent / "latency_results.csv"
    print(f"\nSaving results to {csv_file}...")

    with open(csv_file, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["num_chunks", "latency_ms", "min_ms", "max_ms", "timestamp"]
        )
        writer.writeheader()
        writer.writerows(results)

    print(f"  Wrote {len(results)} measurements to CSV")

    # Generate graph
    try:
        import matplotlib.pyplot as plt
        import matplotlib

        matplotlib.use("Agg")  # Use non-interactive backend

        fig, ax = plt.subplots(figsize=(10, 6))

        chunks = [r["num_chunks"] for r in results]
        latencies = [r["latency_ms"] for r in results]

        # Plot the curve
        ax.plot(chunks, latencies, "b-o", linewidth=2, markersize=8, label="Measured Latency")

        # Add 500ms SLA line
        ax.axhline(y=500, color="r", linestyle="--", linewidth=2, label="500ms SLA")

        # Styling
        ax.set_xlabel("Knowledge Base Size (chunks)", fontsize=12, fontweight="bold")
        ax.set_ylabel("Search Latency (ms)", fontsize=12, fontweight="bold")
        ax.set_title("Embedding Model Latency vs Knowledge Base Scale", fontsize=14, fontweight="bold")
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=10)

        # Log scale for chunks if range is large
        if max(chunks) / min(chunks) > 10:
            ax.set_xscale("log")

        # Add value labels on points
        for x, y in zip(chunks, latencies):
            ax.annotate(f"{y:.0f}ms", (x, y), textcoords="offset points", xytext=(0, 5), ha="center")

        plt.tight_layout()

        graph_file = Path(__file__).parent / "latency_curve.png"
        plt.savefig(graph_file, dpi=150)
        print(f"  Saved graph to {graph_file}")

    except ImportError:
        print("  Warning: matplotlib not available, skipping graph generation")
    except Exception as e:
        print(f"  Error generating graph: {e}")

    # Print summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    for r in results:
        chunks = r["num_chunks"]
        latency = r["latency_ms"]
        status = "✓" if latency <= 500 else "⚠️"
        print(f"{status} {chunks:5d} chunks: {latency:7.2f}ms")

    # Identify breakpoint
    breakpoint = None
    for r in results:
        if r["latency_ms"] > 500:
            breakpoint = r["num_chunks"]
            break

    if breakpoint:
        print(f"\n🔴 BREAKPOINT: Local model breaks at {breakpoint} chunks (> 500ms SLA)")
        print(f"   Recommendation: Use vector DB or cloud API at production scale")
    else:
        max_chunks = results[-1]["num_chunks"]
        print(
            f"\n✅ All tests passed within SLA (up to {max_chunks} chunks)"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
