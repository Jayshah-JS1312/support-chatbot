"""RAG — retrieval-augmented generation, over a real vector database.

The agent cannot explain a rule it has never read. The 30-day window lives
inside start_return(), so the agent can ENFORCE it but cannot CITE it.
This module gives it everything that was only ever in a human's head:

    knowledge/policies/      what the customer is entitled to
    knowledge/rules/         what this agent may and may not do
    knowledge/tone/          how to say it
    knowledge/regulations/   the law the policy is built on

Four kinds of knowledge, not one, because they are used at different
moments. A policy is quoted to the customer. A rule decides whether the
agent acts at all. A tone document shapes the sentence. A regulation is
why the policy cannot simply be waived to make someone happy. Keeping
them apart is what makes `category` worth having as a filter.

Retrieval has three steps, and none of them are magic:

    1. chunk    split each document at its ## headings
    2. embed    turn every chunk into 384 numbers, with a 17 MB model
                that runs on this laptop (see embedder.py)
    3. search   embed the question, ask the database for the nearest chunks

Steps 2 and 3 used to be an OpenAI call and fifteen lines of cosine
similarity. Both are gone: the model is local, and the search is chromadb.
What chromadb buys at this size is not speed — with 48 chunks a Python
loop is fine. It is the index stored properly between runs, ids that make
a rebuild idempotent, and metadata carried alongside every chunk, so a
result can say which shelf it came from.

It also offers to filter by that metadata while it searches, which sounds
obviously good and measurably is not. See `search()`.
"""

import hashlib
import re

import chromadb
from chromadb.api.types import EmbeddingFunction

from support_chatbot import embedder
from support_chatbot import observe
from support_chatbot import CACHE_DIR, KNOWLEDGE_DIR

DOCS = KNOWLEDGE_DIR
STORE = CACHE_DIR / "chroma"
CATEGORIES = ("policies", "rules", "tone", "regulations")


# --------------------------------------------------------------------------
# 1. chunk
# --------------------------------------------------------------------------

def _chunks():
    """One chunk per ## section, labelled with its document and category.

    A whole document is too big to retrieve — ask about damaged parcels and
    you would get the refund timing too. A single sentence is too small to
    stand on its own. A ## section is usually the unit someone wrote as one
    thought, which is why it is also the right unit to retrieve.
    """
    out = []
    for path in sorted(DOCS.glob("*/*.md")):
        category = path.parent.name
        text = path.read_text()
        title = re.match(r"#\s*(.+)", text).group(1).strip()
        for part in re.split(r"\n(?=## )", text):
            body = part.strip()
            if body.startswith("# "):                 # the intro under the H1
                heading = title
            else:
                heading = f"{title} — {body.splitlines()[0].lstrip('# ').strip()}"
            body = body.split("\n", 1)[1].strip() if "\n" in body else ""
            if body:
                out.append({"id": f"{category}/{path.name}#{heading}",
                            "category": category,
                            "source": f"{category}/{path.name}",
                            "heading": heading,
                            "text": body})
    return out


# --------------------------------------------------------------------------
# 2. embed  —  chromadb calls this itself, for documents and for questions
# --------------------------------------------------------------------------

class _Local(EmbeddingFunction):
    """Hand chromadb our own model instead of the one it would download."""

    def __init__(self):
        pass

    def __call__(self, input):
        return embedder.embed(input)

    @staticmethod
    def name():
        return "bge-micro-v2"


# --------------------------------------------------------------------------
# 3. the database
# --------------------------------------------------------------------------

_COLLECTION = None


def collection():
    """The chroma collection, built on first use and rebuilt when docs change.

    Embedding 40 chunks takes about a second, and the result is the same
    every time, so it is written to disk and reused. The stamp is how we
    notice an edit: change a word in any document and the whole index is
    rebuilt, because a stale index is worse than no index — it answers
    confidently out of a file you already fixed.
    """
    global _COLLECTION
    if _COLLECTION is not None:
        return _COLLECTION

    chunks = _chunks()
    # sha256, not hash(): Python randomises string hashing per process, so
    # hash() would disagree with itself on the next run and rebuild the
    # index every time. The JSON cache this replaced did exactly that,
    # quietly, because one wasted embedding call is invisible.
    stamp = hashlib.sha256(
        "".join(sorted(c["id"] + c["text"] for c in chunks)).encode()).hexdigest()[:16]
    client = chromadb.PersistentClient(path=str(STORE))

    existing = {c.name for c in client.list_collections()}
    if "knowledge" in existing:
        found = client.get_collection("knowledge", embedding_function=_Local())
        if found.metadata.get("stamp") == stamp and found.count() == len(chunks):
            _COLLECTION = found
            return _COLLECTION
        client.delete_collection("knowledge")        # the documents moved on

    print(f"  indexing {len(chunks)} chunks from {DOCS.name}/ ...", flush=True)
    _COLLECTION = client.create_collection(
        "knowledge",
        embedding_function=_Local(),
        metadata={"stamp": stamp, "hnsw:space": "cosine"})
    _COLLECTION.add(
        ids=[c["id"] for c in chunks],
        documents=[f"{c['heading']}\n{c['text']}" for c in chunks],
        metadatas=[{"category": c["category"], "source": c["source"],
                    "heading": c["heading"]} for c in chunks])
    return _COLLECTION


# --------------------------------------------------------------------------
# 4. search
# --------------------------------------------------------------------------

def search(question, k=3, category=None, log_event=True):
    """The k chunks nearest the question, best first, across everything.

    `category` restricts the search to one shelf. The agent is NOT given
    it — `search_knowledge` has no category argument — and that is a
    deliberate result rather than an oversight.

    The idea is seductive: a chargeback question is about regulations, so
    search only the regulations and get three regulations instead of three
    near-misses. The catch is that something has to decide the category
    before the search, and the only thing available is the model, guessing
    from a question it has not yet researched. It guesses "policies" for
    almost everything. A filter cannot merely rank the right passage lower
    — it removes it from the running, so a wrong guess is unrecoverable:

        "already disputed the charge with their bank"
            filtered to policies   0.750  policies/cancellations.md    wrong
            no filter              0.885  regulations/refunds-...md    right
        "does a gift card balance expire"
            filtered to policies   0.676  policies/account.md          wrong
            no filter              0.813  regulations/gift-cards.md    right

    And when the guess is right, the filtered result is identical to the
    unfiltered one — the right chunk was winning on its own merits anyway.
    So the filter never helped and sometimes broke it. Nor can a "fall back
    when the score looks weak" rule save it: a wrong-category hit scored
    0.750 and a right-category hit 0.753, so no threshold separates them.

    At 48 chunks, searching everything IS the right answer, and category
    earns its place as a label on the result rather than a filter on the
    search. Keep the argument for inspecting the corpus by hand; revisit
    the filter when there are enough documents for it to pay for itself.
    """
    with observe.timer() as t:
        found = collection().query(
            query_texts=[question],
            n_results=k,
            where={"category": category} if category else None)

    hits = []
    for doc, meta, distance in zip(found["documents"][0],
                                   found["metadatas"][0],
                                   found["distances"][0]):
        hits.append({"heading": meta["heading"], "source": meta["source"],
                     "category": meta["category"],
                     "text": doc.split("\n", 1)[1] if "\n" in doc else doc,
                     # chroma returns cosine DISTANCE; 1 - d is the similarity
                     # everyone actually talks about.
                     "score": round(1 - distance, 3)})

    if log_event:
        observe.log("retrieval", question=question, ms=t.ms, category=category,
                    hits=[{"heading": h["heading"], "score": h["score"]} for h in hits])
    return hits


if __name__ == "__main__":
    # python -m support_chatbot.knowledge "why won't you refund me"
    import sys
    q = " ".join(sys.argv[1:]) or "can I return something after 45 days"
    for h in search(q, k=4):
        print(f"  {h['score']:.3f}  [{h['category']}] {h['heading']}")
