from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from pennyguard import Price, Pricing, UnknownModel, Usage


class TestPrice:
    def test_cost(self) -> None:
        price = Price(1, 5)
        usage = Usage("m", input_tokens=1_000, output_tokens=500)
        assert price.cost(usage) == Decimal("0.0035")

    def test_cache_prices_default_upward(self) -> None:
        price = Price(2, 10)
        reads = Usage("m", cache_read_tokens=1_000_000)
        writes = Usage("m", cache_write_tokens=1_000_000)
        assert price.cost(reads) == Decimal(2)
        assert price.cost(writes) == Decimal("2.5")

    def test_explicit_cache_prices(self) -> None:
        price = Price(2, 10, cache_read_per_mtok="0.2", cache_write_per_mtok="2.5")
        usage = Usage(
            "m",
            input_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_write_tokens=1_000_000,
        )
        assert price.cost(usage) == Decimal("4.7")

    def test_long_context_tier_prices_the_whole_call(self) -> None:
        price = Price(1, 5, above={200_000: Price(2, 10)})
        under = Usage("m", input_tokens=200_000, output_tokens=1_000)
        over = Usage("m", input_tokens=100_000, cache_read_tokens=100_001)
        assert price.cost(under) == Decimal("0.205")
        # Cached tokens count toward the threshold.
        assert price.cost(over) == Decimal("0.400002")

    def test_worst_case_assumes_the_dearest_input_and_full_output(self) -> None:
        # The one-hour cache write defaults to twice the input price.
        price = Price(1, 5, cache_write_per_mtok="1.25")
        assert price.worst_case(1_000_000, 1_000_000) == Decimal(7)
        exact = Price(1, 5, cache_write_per_mtok="1.25", cache_write_1h_per_mtok="1.25")
        assert exact.worst_case(1_000_000, 1_000_000) == Decimal("6.25")
        with pytest.raises(ValueError):
            price.worst_case(-1, 0)

    def test_dearer_rates_need_a_variant(self) -> None:
        price = Price(1, 5, variants={"fast": Price(2, 10)})
        fast = Usage("m", input_tokens=1_000_000, tier="fast")
        assert price.cost(fast) == Decimal(2)
        # A dearer rate with no price is refused, never billed at standard.
        with pytest.raises(UnknownModel, match="priority"):
            price.cost(Usage("m", input_tokens=1, tier="priority"))
        # Flex is cheaper, so standard is a safe overcharge.
        assert price.cost(Usage("m", input_tokens=1_000_000, tier="flex")) == Decimal(1)

    def test_variants_are_hash_safe(self) -> None:
        price = Price(1, 5, variants={"fast": Price(2, 10)})
        assert price == Price(1, 5, variants={"fast": Price(2, 10)})
        assert hash(price) == hash(Price(1, 5, variants={"fast": Price(3, 3)}))

    def test_validation(self) -> None:
        with pytest.raises(ValueError):
            Price(-1, 1, variants={"": Price(1, 1)})
        with pytest.raises(ValueError):
            Price(1, 1, variants={"a": Price(1, 1, variants={"b": Price(1, 1)})})
        with pytest.raises(ValueError):
            Price(-1, 1)
        with pytest.raises(ValueError):
            Price(1, 1, above={0: Price(1, 1)})
        with pytest.raises(ValueError):
            Price(1, 1, above={10: Price(1, 1, above={20: Price(1, 1)})})


class TestPricingLookup:
    table = Pricing(
        {
            "gpt-4o": Price(2.5, 10),
            "gpt-4o-mini": Price(0.15, 0.6),
            "claude-x": Price(1, 5),
        }
    )

    def test_exact(self) -> None:
        assert self.table.price("gpt-4o-mini").input_per_mtok == Decimal("0.15")

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("gpt-4o-2024-08-06", "2.5"),
            ("gpt-4o-mini-2024-07-18", "0.15"),
            ("claude-x-20260101", "1"),
            ("claude-x@20260101", "1"),
            ("claude-x-latest", "1"),
        ],
    )
    def test_versioned_names_resolve_to_their_base(
        self, model: str, expected: str
    ) -> None:
        assert self.table.price(model).input_per_mtok == Decimal(expected)

    @pytest.mark.parametrize("model", ["gpt-4o-audio-preview", "gpt-4", "claude-xl"])
    def test_a_different_model_is_never_a_prefix_match(self, model: str) -> None:
        with pytest.raises(UnknownModel):
            self.table.price(model)
        assert model not in self.table

    def test_default_and_with_prices(self) -> None:
        fallback = Pricing({}, default=Price(100, 100))
        assert fallback.price("anything").input_per_mtok == Decimal(100)
        extended = self.table.with_prices({"new-model": Price(3, 4)})
        assert "new-model" in extended
        assert "new-model" not in self.table
        assert len(extended) == 4

    def test_estimate(self) -> None:
        cost = self.table.estimate(
            "claude-x", input_tokens=1_000, max_output_tokens=1_000
        )
        assert cost == Decimal("0.007")


LITELLM = {
    "sample_spec": {"input_cost_per_token": 0, "output_cost_per_token": 0},
    "claude-x": {
        "litellm_provider": "anthropic",
        "mode": "chat",
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_read_input_token_cost": 3e-07,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_creation_input_token_cost_above_1hr": 6e-06,
        "input_cost_per_token_above_200k_tokens": 6e-06,
        "output_cost_per_token_above_200k_tokens": 2.25e-05,
        "input_cost_per_token_batches": 1.5e-06,
    },
    "gpt-x": {
        "litellm_provider": "openai",
        "mode": "chat",
        "input_cost_per_token": 2.5e-06,
        "output_cost_per_token": 1e-05,
        "cache_read_input_token_cost": 1.25e-06,
        "input_cost_per_token_priority": 4.25e-06,
        "output_cost_per_token_priority": 1.7e-05,
        "input_cost_per_token_flex": 1.25e-06,
        "output_cost_per_token_flex": 5e-06,
    },
    "embed-x": {
        "mode": "embedding",
        "input_cost_per_token": 1e-07,
        "output_cost_per_token": 0,
    },
    "image-x": {"mode": "chat", "output_cost_per_image": 0.04},
}


class TestFromLitellm:
    def test_reads_chat_models_only(self) -> None:
        table = Pricing.from_litellm(LITELLM)
        assert "claude-x" in table
        assert "gpt-x" in table
        assert "embed-x" not in table
        assert "image-x" not in table
        assert "sample_spec" not in table

    def test_prices_per_million_and_tiers(self) -> None:
        price = Pricing.from_litellm(LITELLM).price("claude-x")
        assert price.input_per_mtok == Decimal(3)
        assert price.output_per_mtok == Decimal(15)
        assert price.cache_read_per_mtok == Decimal("0.3")
        assert price.cache_write_per_mtok == Decimal("3.75")
        threshold, tier = price.above[0]
        assert threshold == 200_000
        assert tier.input_per_mtok == Decimal(6)
        assert tier.output_per_mtok == Decimal("22.5")
        # Not listed for the tier: the standard price, scaled up by how much
        # dearer the tier's input is (2x here), as providers price tiers.
        assert tier.cache_read_per_mtok == Decimal("0.6")
        assert tier.cache_write_per_mtok == Decimal("7.5")

    def test_reads_priority_and_flex_rates(self) -> None:
        price = Pricing.from_litellm(LITELLM).price("gpt-x")
        assert set(price.variants) == {"priority", "flex"}
        assert price.variants["priority"].input_per_mtok == Decimal("4.25")
        assert price.variants["priority"].output_per_mtok == Decimal(17)
        # Unlisted fields of a variant scale with its input price (1.7x).
        assert price.variants["priority"].cache_read_per_mtok == Decimal("2.125")
        assert price.variants["flex"].output_per_mtok == Decimal(5)
        assert Pricing.from_litellm(LITELLM).price("claude-x").variants == {}

    def test_no_cache_write_price_means_no_markup(self) -> None:
        price = Pricing.from_litellm(LITELLM).price("gpt-x")
        assert price.cache_write_per_mtok == Decimal("2.5")
        assert price.worst_case(1_000_000, 0) == Decimal("2.5")

    def test_reads_a_file(self, tmp_path: Path) -> None:
        path = tmp_path / "prices.json"
        path.write_text(json.dumps(LITELLM), encoding="utf-8")
        assert "claude-x" in Pricing.from_litellm(path)
        assert "claude-x" in Pricing.from_litellm(str(path))


class TestUsage:
    def test_validation(self) -> None:
        with pytest.raises(ValueError):
            Usage("")
        with pytest.raises(ValueError):
            Usage("m", input_tokens=-1)
        with pytest.raises(ValueError):
            Usage("m", output_tokens=True)
        assert Usage("m", 1, 2, 3, 4).prompt_tokens == 8

    def test_anthropic_object(self) -> None:
        response = NS(
            model="claude-x",
            usage=NS(
                input_tokens=100,
                output_tokens=50,
                cache_read_input_tokens=1_000,
                cache_creation_input_tokens=200,
            ),
        )
        expected = Usage("claude-x", 100, 50, 1_000, 200)
        assert Usage.from_anthropic(response) == expected
        assert Usage.from_response(response) == expected

    def test_anthropic_dict_with_null_cache_fields(self) -> None:
        response = {
            "model": "claude-x",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": None,
                "cache_creation_input_tokens": None,
            },
        }
        assert Usage.from_response(response) == Usage("claude-x", 10, 5)

    def test_all_from_response_reads_every_anthropic_attempt(self) -> None:
        response = NS(
            model="claude-y",
            usage=NS(
                input_tokens=412,
                output_tokens=264,
                iterations=[
                    NS(
                        type="message",
                        model="claude-x",
                        input_tokens=535,
                        output_tokens=0,
                    ),
                    NS(
                        type="fallback_message",
                        model="claude-y",
                        input_tokens=412,
                        output_tokens=264,
                        cache_read_input_tokens=10,
                    ),
                ],
            ),
        )
        assert Usage.all_from_response(response) == [
            Usage("claude-x", 535, 0),
            Usage("claude-y", 412, 264, cache_read_tokens=10),
        ]
        # The top level alone is only the attempt that answered.
        assert Usage.from_anthropic(response) == Usage("claude-y", 412, 264)

    def test_all_from_response_without_iterations(self) -> None:
        anthropic = {"model": "m", "usage": {"input_tokens": 1, "output_tokens": 2}}
        openai = {"model": "m", "usage": {"prompt_tokens": 3, "completion_tokens": 4}}
        assert Usage.all_from_response(anthropic) == [Usage("m", 1, 2)]
        assert Usage.all_from_response(openai) == [Usage("m", 3, 4)]
        empty = {"model": "m", "usage": {"input_tokens": 1, "iterations": []}}
        assert Usage.all_from_response(empty) == [Usage("m", 1, 0)]
        with pytest.raises(ValueError):
            Usage.all_from_response({"model": "m"})

    def test_openai_chat_subtracts_cached_tokens(self) -> None:
        response = NS(
            model="gpt-x",
            usage=NS(
                prompt_tokens=1_000,
                completion_tokens=200,
                prompt_tokens_details=NS(cached_tokens=600),
            ),
        )
        expected = Usage(
            "gpt-x", input_tokens=400, output_tokens=200, cache_read_tokens=600
        )
        assert Usage.from_openai(response) == expected
        assert Usage.from_response(response) == expected

    def test_openai_responses_api(self) -> None:
        response = {
            "model": "gpt-x",
            "usage": {
                "input_tokens": 1_000,
                "output_tokens": 300,
                "input_tokens_details": {"cached_tokens": 100},
                "output_tokens_details": {"reasoning_tokens": 250},
            },
        }
        expected = Usage(
            "gpt-x", input_tokens=900, output_tokens=300, cache_read_tokens=100
        )
        assert Usage.from_response(response) == expected

    def test_rate_tiers_are_read_from_responses(self) -> None:
        fast = {"model": "c", "usage": {"input_tokens": 1, "speed": "fast"}}
        standard = {"model": "c", "usage": {"input_tokens": 1, "speed": "standard"}}
        assert Usage.from_response(fast).tier == "fast"
        assert Usage.from_response(standard).tier is None
        with_iterations = {
            "model": "c",
            "usage": {"speed": "fast", "iterations": [{"input_tokens": 1}]},
        }
        assert Usage.all_from_response(with_iterations)[0].tier == "fast"
        for service_tier, expected in [
            ("priority", "priority"),
            ("flex", "flex"),
            ("default", None),
            (None, None),
        ]:
            response = {
                "model": "g",
                "service_tier": service_tier,
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
            assert Usage.from_response(response).tier == expected

    def test_openai_without_details(self) -> None:
        response = {
            "model": "gpt-x",
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        }
        assert Usage.from_response(response) == Usage("gpt-x", 7, 3)

    @pytest.mark.parametrize(
        "response",
        [
            {"model": "m"},
            {"usage": {"input_tokens": 1}},
            {"model": "m", "usage": {}},
            {"model": "m", "usage": {"input_tokens": "12"}},
            {
                "model": "m",
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 1,
                    "prompt_tokens_details": {"cached_tokens": 6},
                },
            },
        ],
    )
    def test_unreadable_responses_raise_value_error(self, response: dict) -> None:
        with pytest.raises(ValueError):
            Usage.from_response(response)


class TestReviewRegressions:
    """Cases an independent review found, kept so they stay fixed."""

    @pytest.mark.parametrize(
        "model", ["gpt-4-32k", "llama-3-70b", "gpt-4-1106-preview"]
    )
    def test_a_numbered_variant_is_a_different_model(self, model: str) -> None:
        table = Pricing({"gpt-4": Price(30, 60), "llama-3": Price(1, 1)})
        with pytest.raises(UnknownModel):
            table.price(model)

    @pytest.mark.parametrize(
        "model", ["gpt-4-0613", "gpt-4-20240101", "gpt-4@20240101"]
    )
    def test_dated_snapshots_still_resolve(self, model: str) -> None:
        table = Pricing({"gpt-4": Price(30, 60)})
        assert table.price(model).input_per_mtok == Decimal(30)

    def test_estimate_prices_the_requested_rate(self) -> None:
        table = Pricing(
            {
                "m": Price(
                    1, 5, cache_write_1h_per_mtok=1, variants={"fast": Price(6, 30)}
                )
            }
        )
        # A fast-mode variant without cache prices defaults its one-hour write
        # to twice its own input price.
        assert table.estimate("m", input_tokens=1_000, max_output_tokens=1_000) == (
            Decimal("0.00625")
        )
        assert table.estimate(
            "m", input_tokens=1_000, max_output_tokens=1_000, tier="fast"
        ) == Decimal("0.042")
        with pytest.raises(UnknownModel, match="priority"):
            table.estimate("m", input_tokens=1, max_output_tokens=1, tier="priority")

    def test_one_hour_cache_writes_cost_more(self) -> None:
        response = {
            "model": "m",
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 300_000,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 200_000,
                    "ephemeral_1h_input_tokens": 100_000,
                },
            },
        }
        usage = Usage.from_response(response)
        assert (usage.cache_write_tokens, usage.cache_write_1h_tokens) == (
            200_000,
            100_000,
        )
        assert usage.prompt_tokens == 300_000
        price = Price(5, 25, cache_write_per_mtok="6.25", cache_write_1h_per_mtok=10)
        assert price.cost(usage) == Decimal("2.25")
        # Defaults: 1.25x and 2x the input price.
        assert Price(5, 25).cost(usage) == Decimal("2.25")

    def test_an_inconsistent_cache_breakdown_is_unreadable(self) -> None:
        response = {
            "model": "m",
            "usage": {
                "cache_creation_input_tokens": 10,
                "cache_creation": {"ephemeral_1h_input_tokens": 11},
            },
        }
        with pytest.raises(ValueError):
            Usage.from_response(response)

    def test_litellm_one_hour_cache_writes(self) -> None:
        table = Pricing.from_litellm(LITELLM)
        assert table.price("claude-x").cache_write_1h_per_mtok == Decimal(6)
        # No one-hour price listed: the provider bills no such writes.
        assert table.price("gpt-x").cache_write_1h_per_mtok == Decimal("2.5")
        assert table.price("gpt-x").worst_case(1_000_000, 0) == Decimal("2.5")


def _litellm_entry(**fields: float) -> dict[str, object]:
    return {"mode": "chat", **fields}


class TestAuditRegressions:
    def test_litellm_one_hour_writes_above_a_tier(self) -> None:
        entry = _litellm_entry(
            input_cost_per_token=3e-06,
            output_cost_per_token=1.5e-05,
            cache_creation_input_token_cost=3.75e-06,
            cache_creation_input_token_cost_above_1hr=6e-06,
            input_cost_per_token_above_200k_tokens=6e-06,
            output_cost_per_token_above_200k_tokens=2.25e-05,
            cache_creation_input_token_cost_above_1hr_above_200k_tokens=1.2e-05,
        )
        table = Pricing.from_litellm({"c": entry})
        usage = Usage("c", input_tokens=1_000, cache_write_1h_tokens=250_000)
        # 1k input at $6 and 250k one-hour writes at $12, not at $6.
        assert table.cost(usage) == Decimal("3.006")
        assert table.estimate(
            "c", input_tokens=250_000, max_output_tokens=0
        ) == Decimal(3)
        # Without its own one-hour price, the tier scales the standard one.
        del entry["cache_creation_input_token_cost_above_1hr_above_200k_tokens"]
        tier = Pricing.from_litellm({"c": entry}).price("c").above[0][1]
        assert tier.cache_write_1h_per_mtok == Decimal(12)

    def test_litellm_variants_have_their_own_tiers(self) -> None:
        entry = _litellm_entry(
            input_cost_per_token=2.5e-06,
            output_cost_per_token=1.5e-05,
            input_cost_per_token_above_272k_tokens=5e-06,
            output_cost_per_token_above_272k_tokens=2.25e-05,
            input_cost_per_token_flex=1.25e-06,
            output_cost_per_token_flex=7.5e-06,
            input_cost_per_token_above_272k_tokens_flex=2.5e-06,
            input_cost_per_token_priority=5e-06,
            output_cost_per_token_priority=3e-05,
            input_cost_per_token_above_272k_tokens_priority=1e-05,
        )
        table = Pricing.from_litellm({"g": entry})

        def cost(tier: str | None) -> Decimal:
            return table.cost(Usage("g", input_tokens=300_000, tier=tier))

        assert cost(None) == Decimal("1.5")
        assert cost("priority") == Decimal(3)
        assert cost("flex") == Decimal("0.75")

    def test_a_long_prompt_at_a_variant_without_tiers(self) -> None:
        price = Price(
            2.5,
            15,
            above={272_000: Price(5, 22.5)},
            variants={"priority": Price(5, 30), "flex": Price("1.25", "7.5")},
        )
        long = 300_000
        with pytest.raises(UnknownModel, match="above 272000"):
            price.cost(Usage("g", input_tokens=long, tier="priority"))
        # Flex is cheaper: without a tier of its own it pays the standard one.
        assert price.cost(Usage("g", input_tokens=long, tier="flex")) == Decimal("1.5")
        assert price.cost(Usage("g", input_tokens=1_000, tier="priority")) == (
            Decimal("0.005")
        )

    def test_openai_audio_tokens_are_split_out(self) -> None:
        response = {
            "model": "gpt-audio",
            "usage": {
                "prompt_tokens": 1_000,
                "completion_tokens": 600,
                "prompt_tokens_details": {"cached_tokens": 100, "audio_tokens": 300},
                "completion_tokens_details": {"audio_tokens": 500},
            },
        }
        usage = Usage.from_response(response)
        assert usage == Usage(
            "gpt-audio",
            input_tokens=600,
            output_tokens=100,
            cache_read_tokens=100,
            input_audio_tokens=300,
            output_audio_tokens=500,
        )
        assert usage.prompt_tokens == 1_000
        price = Price(
            "2.5",
            10,
            cache_read_per_mtok="1.25",
            input_audio_per_mtok=40,
            output_audio_per_mtok=80,
        )
        # 600 x 2.5 + 100 x 10 + 100 x 1.25 + 300 x 40 + 500 x 80
        assert price.cost(usage) == Decimal("0.054625")
        with pytest.raises(UnknownModel, match="audio"):
            Price("2.5", 10).cost(usage)

    def test_inconsistent_audio_counts_are_unreadable(self) -> None:
        response = {
            "model": "m",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 6, "audio_tokens": 5},
            },
        }
        with pytest.raises(ValueError):
            Usage.from_response(response)

    def test_litellm_audio_prices_and_audio_estimates(self) -> None:
        entry = _litellm_entry(
            input_cost_per_token=2.5e-06,
            output_cost_per_token=1e-05,
            input_cost_per_audio_token=4e-05,
            output_cost_per_audio_token=8e-05,
        )
        table = Pricing.from_litellm({"a": entry})
        price = table.price("a")
        assert (price.input_audio_per_mtok, price.output_audio_per_mtok) == (
            Decimal(40),
            Decimal(80),
        )
        text = table.estimate("a", input_tokens=1_000, max_output_tokens=1_000)
        audio = table.estimate(
            "a", input_tokens=1_000, max_output_tokens=1_000, audio=True
        )
        assert (text, audio) == (Decimal("0.0125"), Decimal("0.12"))
        with pytest.raises(UnknownModel, match="audio"):
            Pricing({"t": Price(1, 2)}).estimate(
                "t", input_tokens=1, max_output_tokens=1, audio=True
            )

    def test_an_attempt_without_a_model_after_a_fallback_is_unpriceable(
        self,
    ) -> None:
        response = NS(
            model="cheap-fallback",
            usage=NS(
                input_tokens=5,
                output_tokens=5,
                iterations=[
                    NS(type="message", model=None, input_tokens=100, output_tokens=0),
                    NS(
                        type="fallback_message",
                        model="cheap-fallback",
                        input_tokens=100,
                        output_tokens=50,
                    ),
                ],
            ),
        )
        # The declined attempt ran on a model the response does not name.
        # Pricing it at the fallback's rate could undercount.
        with pytest.raises(ValueError, match="no model"):
            Usage.all_from_response(response)

    def test_malformed_iterations_are_unreadable(self) -> None:
        # ValueError, which settle_response turns into "keep the estimate";
        # anything else would raise after the call was paid for.
        response = {"model": "m", "usage": {"input_tokens": 1, "iterations": 3}}
        with pytest.raises(ValueError, match="not a list"):
            Usage.all_from_response(response)

    def test_an_attempt_without_a_model_on_a_single_model_response(self) -> None:
        response = {
            "model": "m",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "iterations": [
                    {"type": "compaction", "input_tokens": 50, "output_tokens": 5},
                    {"type": "message", "input_tokens": 10, "output_tokens": 5},
                ],
            },
        }
        assert Usage.all_from_response(response) == [
            Usage("m", 50, 5),
            Usage("m", 10, 5),
        ]
