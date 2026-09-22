"""The embedding model — 17 MB, downloaded once, run on this laptop.

An embedding is a list of numbers that stands for a piece of text, arranged
so that two texts about the same thing land near each other. That is the
whole trick behind retrieval: turn the question into numbers, turn every
policy paragraph into numbers, and keep the paragraphs whose numbers are
closest.

Until now we borrowed OpenAI's embedding model through the class proxy. It
worked, but it hid the mechanism behind an API call, and it billed us per
search. So here is the same thing, local:

    TaylorAI/bge-micro-v2, quantized to int8       17 MB on disk
    3 transformer layers, 384 numbers per text     ~5 ms per search

It is a distilled, shrunken version of BGE, a model built specifically for
retrieval — which is why a model this small still beats a much larger
general-purpose one at finding the right paragraph.

Nothing new is installed for this. `onnxruntime` runs the model and
`tokenizers` chops the text into the pieces it expects, and chromadb
already depends on both. The only new bytes on disk are the model itself.

Four steps, all visible below:

    1. tokenize   text  ->  the integer ids the model was trained on
    2. run        ids   ->  one 384-number vector PER TOKEN
    3. pool       many token vectors -> one vector for the whole text
    4. normalize  scale it to length 1, so comparing two is a dot product
"""

import os
import urllib.request

import numpy as np

# ONNX Runtime's optional platform telemetry creates a ``:memory:.ses`` file
# in the process working directory on macOS.  The chatbot does not use that
# telemetry, so disable it before importing onnxruntime rather than hiding a
# recurring runtime artifact with .gitignore alone.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

import onnxruntime
from tokenizers import Tokenizer
from support_chatbot import CACHE_DIR

REPO = "TaylorAI/bge-micro-v2"
FILES = {"model.onnx": "onnx/model_quantized.onnx",   # 17.4 MB, int8
         "tokenizer.json": "tokenizer.json"}          # 700 KB
HOME = CACHE_DIR / "models" / "bge-micro-v2"
MAX_TOKENS = 512          # what this model was trained to accept
DIMS = 384                # numbers per vector


# --------------------------------------------------------------------------
# download, once
# --------------------------------------------------------------------------

def _files():
    """Fetch the model the first time it is asked for, then never again."""
    HOME.mkdir(parents=True, exist_ok=True)
    for local, remote in FILES.items():
        path = HOME / local
        if path.exists():
            continue
        url = f"https://huggingface.co/{REPO}/resolve/main/{remote}"
        print(f"  downloading {REPO}/{remote} ...", flush=True)
        tmp = path.with_suffix(path.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)          # a partial download
        tmp.rename(path)                              # must not look complete
        print(f"  saved {path.name}  ({path.stat().st_size / 1e6:.1f} MB)")
    return HOME / "model.onnx", HOME / "tokenizer.json"


_session = _tokenizer = None


def _load():
    """Start the model. Costs ~200 ms, so we do it once and hold on to it."""
    global _session, _tokenizer
    if _session is None:
        model, vocab = _files()
        _session = onnxruntime.InferenceSession(str(model))
        _tokenizer = Tokenizer.from_file(str(vocab))
        _tokenizer.enable_truncation(max_length=MAX_TOKENS)
        _tokenizer.enable_padding()      # a batch has to be one rectangle
    return _session, _tokenizer


# --------------------------------------------------------------------------
# embed
# --------------------------------------------------------------------------

def embed(texts):
    """Text in, one 384-number vector out per text."""
    session, tokenizer = _load()
    encoded = tokenizer.encode_batch(list(texts))

    ids = np.array([e.ids for e in encoded], dtype=np.int64)
    mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
    available = {"input_ids": ids,
                 "attention_mask": mask,
                 "token_type_ids": np.zeros_like(ids)}   # one segment, always
    feed = {i.name: available[i.name] for i in session.get_inputs()}

    tokens = session.run(None, feed)[0]        # (texts, tokens, 384)

    # 3. Pool. The model gives a vector per TOKEN; we want one per text, so
    #    average them — skipping the padding, which means nothing and would
    #    drag every short text toward the same place.
    weights = mask[:, :, None].astype(np.float32)
    pooled = (tokens * weights).sum(axis=1) / np.maximum(weights.sum(axis=1), 1e-9)

    # 4. Normalize to length 1. Then "how similar" is just a dot product,
    #    and no text counts for more merely because it is longer.
    pooled /= np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9)
    return pooled.astype(np.float32).tolist()


def similarity(a, b):
    """Cosine similarity of two texts, 0 to 1. Handy for seeing it work."""
    va, vb = embed([a, b])
    return float(np.dot(va, vb))


if __name__ == "__main__":
    # python3 -m support_chatbot.embedder — watch the model separate meaning from wording.
    pairs = [
        ("How long do I have to send something back?", "Returns are accepted within 30 days of delivery."),
        ("How long do I have to send something back?", "An order can be cancelled before it ships."),
        ("my parcel never turned up", "The package is missing after a delivered scan."),
        ("my parcel never turned up", "Gift cards cannot be returned."),
    ]
    for q, doc in pairs:
        print(f"{similarity(q, doc):.3f}  {q!r}\n       vs {doc!r}")
