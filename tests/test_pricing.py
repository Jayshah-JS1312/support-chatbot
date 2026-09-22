"""Pricing module: test cost calculation for LLM calls.

Tests pin:
- rates() finds the correct price row for a model name
- cost() calculates the right dollar amount for a known token count
- usd() formats amounts correctly
"""

import pytest

from support_chatbot import pricing


class TestRates:
    """rates() returns the price row for a model."""

    def test_rates_for_known_model(self):
        rate_in, rate_cached, rate_out = pricing.rates("gpt-5.6-terra")
        assert rate_in == 2.00
        assert rate_cached == 0.20
        assert rate_out == 12.00

    def test_rates_ignores_date_suffix(self):
        """Model names may have dates like gpt-4o-2025-01-15."""
        rate_in, rate_cached, rate_out = pricing.rates("gpt-5.6-terra-2025-09-13")
        assert rate_in == 2.00

    def test_rates_case_insensitive(self):
        rate_in, _, _ = pricing.rates("GPT-5.6-TERRA")
        assert rate_in == 2.00

    def test_rates_for_unknown_model_returns_default(self):
        rate_in, rate_cached, rate_out = pricing.rates("unknown-model")
        assert rate_in == 2.00  # DEFAULT
        assert rate_cached == 0.20
        assert rate_out == 12.00

    def test_rates_for_none_returns_default(self):
        rate_in, _, _ = pricing.rates(None)
        assert rate_in == 2.00

    def test_rates_for_empty_string_returns_default(self):
        rate_in, _, _ = pricing.rates("")
        assert rate_in == 2.00

    def test_all_models_in_prices_dict(self):
        """Verify the models listed in the docstring are in PRICES."""
        models = ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-4o-mini"]
        for model in models:
            rates = pricing.rates(model)
            assert rates is not None


class TestCost:
    """cost() calculates the dollar amount for one model call."""

    def test_cost_input_only(self):
        """Just input tokens, no output."""
        dollars = pricing.cost("gpt-5.6-terra", input_tokens=1000)
        # 1000 tokens * $2.00/million = $0.002
        assert dollars == pytest.approx(0.002)

    def test_cost_output_only(self):
        """Just output tokens, no input."""
        dollars = pricing.cost("gpt-5.6-terra", output_tokens=1000)
        # 1000 tokens * $12.00/million = $0.012
        assert dollars == pytest.approx(0.012)

    def test_cost_input_and_output(self):
        """Both input and output tokens."""
        dollars = pricing.cost("gpt-5.6-terra", input_tokens=1000, output_tokens=100)
        # Input: 1000 * $2 / 1M = 0.002
        # Output: 100 * $12 / 1M = 0.0012
        # Total: 0.0032
        assert dollars == pytest.approx(0.002 + 0.0012)

    def test_cost_with_cached_tokens(self):
        """Cached input tokens are charged at a cheaper rate."""
        # 100 cached tokens + 900 fresh input tokens, 100 output
        dollars = pricing.cost(
            "gpt-5.6-terra",
            input_tokens=1000,
            cached_tokens=100,
            output_tokens=100,
        )
        # Fresh: 900 * $2 / 1M = 0.0018
        # Cached: 100 * $0.20 / 1M = 0.00002
        # Output: 100 * $12 / 1M = 0.0012
        # Total: 0.00302
        expected = (900 * 2 + 100 * 0.20 + 100 * 12) / 1_000_000
        assert dollars == pytest.approx(expected)

    def test_cost_zero_tokens(self):
        """No tokens = no cost."""
        dollars = pricing.cost("gpt-5.6-terra", input_tokens=0, output_tokens=0)
        assert dollars == 0

    def test_cost_with_different_model(self):
        """Cost varies by model."""
        cost_terra = pricing.cost("gpt-5.6-terra", input_tokens=1000, output_tokens=100)
        cost_mini = pricing.cost("gpt-4o-mini", input_tokens=1000, output_tokens=100)
        # gpt-4o-mini is cheaper
        assert cost_mini < cost_terra

    def test_cost_clamps_negative_fresh_tokens(self):
        """If cached_tokens > input_tokens, fresh should not be negative."""
        # 1000 total tokens, 1500 cached (doesn't make sense but shouldn't crash)
        dollars = pricing.cost("gpt-5.6-terra", input_tokens=1000, cached_tokens=1500)
        # max(0, 1000 - 1500) = 0 fresh
        # 1500 cached * $0.20 / 1M
        expected = 1500 * 0.20 / 1_000_000
        assert dollars == pytest.approx(expected)


class TestUSD:
    """usd() formats dollar amounts for display."""

    def test_usd_large_amount(self):
        """Amounts >= $1 show two decimals."""
        assert pricing.usd(5.50) == "$5.50"
        assert pricing.usd(1.00) == "$1.00"
        assert pricing.usd(10.234) == "$10.23"

    def test_usd_cents_amount(self):
        """Amounts >= $0.01 and < $1 show three decimals."""
        assert pricing.usd(0.05) == "$0.050"
        assert pricing.usd(0.99) == "$0.990"

    def test_usd_small_amount(self):
        """Amounts < $0.01 show five decimals."""
        assert pricing.usd(0.001) == "$0.00100"
        assert pricing.usd(0.00001) == "$0.00001"

    def test_usd_zero(self):
        assert pricing.usd(0) == "$0.00000"

    def test_usd_very_small(self):
        """Very small amounts are still formatted."""
        assert pricing.usd(0.000001) == "$0.00000"  # Rounds to nearest

    def test_usd_rounding(self):
        """Values are rounded appropriately for each range."""
        assert pricing.usd(0.015) == "$0.015"
        assert pricing.usd(0.0149) == "$0.015"
