"""What a call costs.

Prices are dollars per MILLION tokens. Input and output are billed at
different rates — output is usually 5-10x input — so the trace has to
record the two separately or the cost number is meaningless.

VERIFY THESE BEFORE QUOTING THEM. Published third-party figures for
gpt-5.6-terra disagree ($2/$12 vs $2.50/$15), OpenAI's own pricing page
refused to load, and the class proxy may bill differently from list price
or not at all. Treat every dollar figure here as an estimate whose job is
to show RELATIVE cost — which turns are expensive, and why.
"""

PRICES = {
    #  model                     input   cached-in   output
    "gpt-5.6-terra":            (2.00,     0.20,     12.00),
    "gpt-5.6-sol":              (5.00,     0.50,     30.00),
    "gpt-5.6-luna":             (0.20,     0.02,      1.20),
    "gpt-4o-mini":              (0.15,     0.075,     0.60),
}

DEFAULT = (2.00, 0.20, 12.00)      # used when a model name is unknown


def rates(model):
    """Find the price row for a model, ignoring any date suffix."""
    if not model:
        return DEFAULT
    name = model.lower()
    for known, row in PRICES.items():
        if name.startswith(known):
            return row
    return DEFAULT


def cost(model, input_tokens=0, output_tokens=0, cached_tokens=0):
    """Dollars for one call. Cached input is billed at the cheaper rate."""
    rate_in, rate_cached, rate_out = rates(model)
    fresh = max(0, (input_tokens or 0) - (cached_tokens or 0))
    return (fresh * rate_in
            + (cached_tokens or 0) * rate_cached
            + (output_tokens or 0) * rate_out) / 1_000_000


def usd(amount):
    """Format small dollar amounts so they stay readable."""
    if amount >= 1:
        return f"${amount:,.2f}"
    if amount >= 0.01:
        return f"${amount:.3f}"
    return f"${amount:.5f}"
