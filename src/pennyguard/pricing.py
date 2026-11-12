"""Model prices and the cost of a call.

pennyguard ships no prices. A bundled table is out of date the week a
provider changes one, and a stale price in a spending cap undercharges
silently. Prices come from you, or from LiteLLM's community-maintained
``model_prices_and_context_window.json``, which ``Pricing.from_litellm``
reads.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pennyguard.errors import UnknownModel
from pennyguard.money import Amount, to_decimal
from pennyguard.usage import Usage

_MILLION = Decimal(1_000_000)
_CACHE_WRITE_MARKUP = Decimal("1.25")
_CACHE_WRITE_1H_MARKUP = Decimal(2)

# A versioned name resolves to its base entry only when the rest is a dated
# snapshot or "latest": "gpt-4o-2024-08-06" -> "gpt-4o",
# "claude-x-20250929" or "claude-x@20250929" -> "claude-x", "gpt-4-0613" ->
# "gpt-4". Never "gpt-4o-mini" -> "gpt-4o", "gpt-4-32k" -> "gpt-4" or
# "llama-3-70b" -> "llama-3": those are different models at different prices.
_VERSION_SUFFIX = re.compile(r"(?:[-@](?:\d{8}|\d{4}-\d{2}-\d{2}|\d{4})|-latest)")
_TIER_FIELD = re.compile(
    r"^(input_cost_per_token|output_cost_per_token|cache_read_input_token_cost"
    r"|cache_creation_input_token_cost(?:_above_1hr)?"
    r"|input_cost_per_audio_token|output_cost_per_audio_token)"
    r"_above_(\d+)k_tokens$"
)
_LITELLM_MODES = frozenset({"chat", "completion", "responses"})
_LITELLM_VARIANTS = ("priority", "flex")
_CHEAPER_THAN_STANDARD = frozenset({"flex"})


def _per_mtok(value: Amount | None, name: str) -> Decimal | None:
    if value is None:
        return None
    result = to_decimal(value, name)
    if result < 0:
        raise ValueError(f"{name} must not be negative, got {value!r}")
    return result


@dataclass(frozen=True)
class Price:
    """Prices in dollars per million tokens.

    Missing cache prices default upward, never downward: a cache read costs
    the input price, a cache write 1.25 times it, and a one-hour cache write
    (Anthropic's ``ttl: "1h"``) twice it. Pass the real numbers to stop
    overcharging.

    ``above`` holds long-context tiers as ``(threshold, Price)`` pairs. When a
    call's prompt (cached tokens included) is larger than the threshold, the
    whole call is priced at that tier, which is how providers bill it.

    ``variants`` prices the non-standard rates a call can report in
    ``Usage.tier``, such as ``{"fast": Price(...)}``. A call at a dearer rate
    with no variant here is not priced at the standard rate, which would
    undercharge: it raises ``UnknownModel`` and the guard keeps the estimate.
    A variant can have tiers of its own, and the same holds for a long
    prompt at a variant with no tier for it. ``"flex"`` is cheaper than
    standard, so where it has no price of its own it is charged at the
    standard one.

    Audio tokens are priced from ``input_audio_per_mtok`` and
    ``output_audio_per_mtok``. A call that reports audio with no audio price
    raises ``UnknownModel`` too: audio costs several times what text does.
    """

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal | None = None
    cache_write_per_mtok: Decimal | None = None
    above: tuple[tuple[int, Price], ...] = ()
    variants: Mapping[str, Price] = field(default_factory=dict, hash=False)
    cache_write_1h_per_mtok: Decimal | None = None
    input_audio_per_mtok: Decimal | None = None
    output_audio_per_mtok: Decimal | None = None

    def __init__(
        self,
        input_per_mtok: Amount,
        output_per_mtok: Amount,
        cache_read_per_mtok: Amount | None = None,
        cache_write_per_mtok: Amount | None = None,
        above: Mapping[int, Price] | tuple[tuple[int, Price], ...] = (),
        variants: Mapping[str, Price] | None = None,
        cache_write_1h_per_mtok: Amount | None = None,
        input_audio_per_mtok: Amount | None = None,
        output_audio_per_mtok: Amount | None = None,
    ) -> None:
        tiers = tuple(sorted(dict(above).items()))
        for threshold, tier in tiers:
            if threshold <= 0:
                raise ValueError(f"tier threshold must be positive, got {threshold}")
            if tier.above:
                raise ValueError("a tier cannot have tiers of its own")
        for name, variant in (variants or {}).items():
            if not name:
                raise ValueError("variant names must not be empty")
            if variant.variants:
                raise ValueError("a variant cannot have variants of its own")
        object.__setattr__(self, "variants", MappingProxyType(dict(variants or {})))
        object.__setattr__(
            self, "input_per_mtok", _per_mtok(input_per_mtok, "input_per_mtok")
        )
        object.__setattr__(
            self, "output_per_mtok", _per_mtok(output_per_mtok, "output_per_mtok")
        )
        object.__setattr__(
            self,
            "cache_read_per_mtok",
            _per_mtok(cache_read_per_mtok, "cache_read_per_mtok"),
        )
        object.__setattr__(
            self,
            "cache_write_per_mtok",
            _per_mtok(cache_write_per_mtok, "cache_write_per_mtok"),
        )
        object.__setattr__(
            self,
            "cache_write_1h_per_mtok",
            _per_mtok(cache_write_1h_per_mtok, "cache_write_1h_per_mtok"),
        )
        object.__setattr__(
            self,
            "input_audio_per_mtok",
            _per_mtok(input_audio_per_mtok, "input_audio_per_mtok"),
        )
        object.__setattr__(
            self,
            "output_audio_per_mtok",
            _per_mtok(output_audio_per_mtok, "output_audio_per_mtok"),
        )
        object.__setattr__(self, "above", tiers)

    def _tier(self, prompt_tokens: int) -> Price:
        chosen = self
        for threshold, tier in self.above:
            if prompt_tokens > threshold:
                chosen = tier
        return chosen

    def _cache_read(self) -> Decimal:
        if self.cache_read_per_mtok is not None:
            return self.cache_read_per_mtok
        return self.input_per_mtok

    def _cache_write(self) -> Decimal:
        if self.cache_write_per_mtok is not None:
            return self.cache_write_per_mtok
        return self.input_per_mtok * _CACHE_WRITE_MARKUP

    def _cache_write_1h(self) -> Decimal:
        if self.cache_write_1h_per_mtok is not None:
            return self.cache_write_1h_per_mtok
        return self.input_per_mtok * _CACHE_WRITE_1H_MARKUP

    def _threshold(self, prompt_tokens: int) -> int:
        return max((t for t, _ in self.above if prompt_tokens > t), default=0)

    def _for(self, tier: str | None, prompt_tokens: int, model: str) -> Price:
        """The rates for a call at ``tier`` with a prompt of this size."""
        standard = self._tier(prompt_tokens)
        if tier is None:
            return standard
        cheaper = tier in _CHEAPER_THAN_STANDARD
        variant = self.variants.get(tier)
        if variant is None:
            if cheaper:
                return standard
            raise UnknownModel(f"{model} at the {tier!r} rate")
        threshold = self._threshold(prompt_tokens)
        if variant._threshold(prompt_tokens) < threshold:
            # A long prompt at a rate with no long-context price of its own.
            if cheaper:
                return standard
            raise UnknownModel(
                f"{model} at the {tier!r} rate above {threshold} prompt tokens"
            )
        return variant._tier(prompt_tokens)

    def _input_audio(self, model: str) -> Decimal:
        if self.input_audio_per_mtok is None:
            raise UnknownModel(f"{model} audio input")
        return self.input_audio_per_mtok

    def _output_audio(self, model: str) -> Decimal:
        if self.output_audio_per_mtok is None:
            raise UnknownModel(f"{model} audio output")
        return self.output_audio_per_mtok

    def cost(self, usage: Usage) -> Decimal:
        """Dollars billed for ``usage`` at this price."""
        p = self._for(usage.tier, usage.prompt_tokens, usage.model)
        total = (
            usage.input_tokens * p.input_per_mtok
            + usage.output_tokens * p.output_per_mtok
            + usage.cache_read_tokens * p._cache_read()
            + usage.cache_write_tokens * p._cache_write()
            + usage.cache_write_1h_tokens * p._cache_write_1h()
        )
        if usage.input_audio_tokens:
            total += usage.input_audio_tokens * p._input_audio(usage.model)
        if usage.output_audio_tokens:
            total += usage.output_audio_tokens * p._output_audio(usage.model)
        return total / _MILLION

    def worst_case(
        self,
        input_tokens: int,
        max_output_tokens: int,
        tier: str | None = None,
        *,
        audio: bool = False,
    ) -> Decimal:
        """An upper bound for one call that has not happened yet.

        Every input token is priced at the dearest way it can be billed
        (plain input, a cache write, a one-hour cache write), and output at
        ``max_output_tokens``, which caps thinking and text together.
        ``tier`` selects a variant such as ``"fast"``. With ``audio``, input
        and output are priced at the audio rate where it is dearer.
        """
        return self._worst_case(
            input_tokens, max_output_tokens, tier, audio=audio, model="this model"
        )

    def _worst_case(
        self,
        input_tokens: int,
        max_output_tokens: int,
        tier: str | None,
        *,
        audio: bool,
        model: str,
    ) -> Decimal:
        if input_tokens < 0 or max_output_tokens < 0:
            raise ValueError("token counts must not be negative")
        p = self._for(tier, input_tokens, model)
        per_input = max(p.input_per_mtok, p._cache_write(), p._cache_write_1h())
        per_output = p.output_per_mtok
        if audio:
            per_input = max(per_input, p._input_audio(model))
            if p.output_audio_per_mtok is not None:
                per_output = max(per_output, p.output_audio_per_mtok)
        total = input_tokens * per_input + max_output_tokens * per_output
        return total / _MILLION


class Pricing:
    """A price table keyed by model name."""

    def __init__(
        self, prices: Mapping[str, Price], *, default: Price | None = None
    ) -> None:
        for model in prices:
            if not model:
                raise ValueError("model names must not be empty")
        self._prices = dict(prices)
        self._default = default

    def __contains__(self, model: object) -> bool:
        return isinstance(model, str) and self._resolve(model) is not None

    def __len__(self) -> int:
        return len(self._prices)

    def with_prices(self, prices: Mapping[str, Price]) -> Pricing:
        """A copy with ``prices`` added or replaced.

        For a model released after the table you loaded, or a negotiated
        rate.
        """
        return Pricing({**self._prices, **prices}, default=self._default)

    def _resolve(self, model: str) -> Price | None:
        exact = self._prices.get(model)
        if exact is not None:
            return exact
        best: str | None = None
        for name in self._prices:
            if (
                model.startswith(name)
                and _VERSION_SUFFIX.fullmatch(model[len(name) :])
                and (best is None or len(name) > len(best))
            ):
                best = name
        return self._prices[best] if best is not None else self._default

    def price(self, model: str) -> Price:
        """The price for ``model``. Raises ``UnknownModel`` if there is none."""
        found = self._resolve(model)
        if found is None:
            raise UnknownModel(model)
        return found

    def cost(self, usage: Usage) -> Decimal:
        return self.price(usage.model).cost(usage)

    def estimate(
        self,
        model: str,
        *,
        input_tokens: int,
        max_output_tokens: int,
        tier: str | None = None,
        audio: bool = False,
    ) -> Decimal:
        """Worst-case cost of one call, to pass as ``estimate_usd``.

        Pass ``audio=True`` when the call sends or returns audio.
        """
        return self.price(model)._worst_case(
            input_tokens, max_output_tokens, tier, audio=audio, model=model
        )

    @classmethod
    def from_litellm(
        cls,
        source: Mapping[str, Any] | str | os.PathLike[str],
        *,
        default: Price | None = None,
    ) -> Pricing:
        """Load LiteLLM's ``model_prices_and_context_window.json``.

        ``source`` is the parsed JSON or a path to the file. Only chat and
        completion models with token prices are kept. Read: long-context
        tiers (``*_above_200k_tokens`` and similar), one-hour cache writes
        (``*_above_1hr``), audio token prices, and the ``priority`` and
        ``flex`` rates as variants, each with its own tiers. A price an entry
        leaves out for a tier or variant is the standard one, scaled up by
        how much dearer that tier's input is. Batch rates are not read: a
        batch call is billed at the standard rate here, which overcounts.
        Cached audio is priced as cached text, because responses do not
        report it apart.
        """
        if isinstance(source, Mapping):
            data: Mapping[str, Any] = source
        else:
            data = json.loads(Path(source).read_text(encoding="utf-8"))

        prices: dict[str, Price] = {}
        for model, entry in data.items():
            if model == "sample_spec" or not isinstance(entry, Mapping):
                continue
            mode = entry.get("mode")
            if mode is not None and mode not in _LITELLM_MODES:
                continue
            per_token_in = entry.get("input_cost_per_token")
            per_token_out = entry.get("output_cost_per_token")
            if per_token_in is None or per_token_out is None:
                continue
            prices[model] = _price_from_litellm(entry)
        return cls(prices, default=default)


def _mtok(per_token: Any) -> Decimal | None:
    if per_token is None:
        return None
    return to_decimal(per_token, "price") * _MILLION


def _price_from_litellm(entry: Mapping[str, Any]) -> Price:
    base = _litellm_rates(entry, None)
    variants: dict[str, Price] = {}
    for name in _LITELLM_VARIANTS:
        suffix = f"_{name}"
        fields = {
            key.removesuffix(suffix): value
            for key, value in entry.items()
            if key.endswith(suffix)
        }
        if "input_cost_per_token" in fields or "output_cost_per_token" in fields:
            variants[name] = _litellm_rates(fields, base)
    return replace(base, variants=variants) if variants else base


def _litellm_rates(fields: Mapping[str, Any], base: Price | None) -> Price:
    """The flat rates in ``fields`` plus their long-context tiers."""
    flat = _litellm_flat(fields, base)
    tiers: dict[int, dict[str, Any]] = {}
    for name, value in fields.items():
        match = _TIER_FIELD.match(name)
        if match:
            tiers.setdefault(int(match.group(2)) * 1_000, {})[match.group(1)] = value
    if not tiers:
        return flat
    return replace(
        flat,
        above=tuple((t, _litellm_flat(f, flat)) for t, f in tiers.items()),
    )


def _litellm_flat(fields: Mapping[str, Any], base: Price | None) -> Price:
    """One set of rates. A rate ``fields`` leaves out comes from ``base``,
    scaled by how much dearer this input is than ``base``'s (never down)."""

    def own(key: str) -> Decimal | None:
        return _mtok(fields.get(key))

    input_price = own("input_cost_per_token")
    if input_price is None and base is not None:
        input_price = base.input_per_mtok
    if input_price is None:
        raise ValueError("entry has no input price")
    scale = Decimal(1)
    if base is not None and base.input_per_mtok > 0:
        scale = max(scale, input_price / base.input_per_mtok)

    def inherited(
        key: str, from_base: Decimal | None, floor: Decimal | None = None
    ) -> Decimal | None:
        value = own(key)
        if value is None and from_base is not None:
            value = from_base * scale
            if floor is not None:
                value = max(value, floor)
        return value

    output_price = inherited(
        "output_cost_per_token", base.output_per_mtok if base else None
    )
    if output_price is None:
        raise ValueError("entry has no output price")
    # A cache write inherited by a tier or variant never falls below its own
    # input price.
    cache_write = inherited(
        "cache_creation_input_token_cost",
        base.cache_write_per_mtok if base else None,
        floor=input_price,
    )
    # The table lists a cache-write price for every provider that charges
    # one. Absent means the provider bills no writes, so the defaults meant
    # for hand-written prices would only inflate every estimate.
    if cache_write is None:
        cache_write = input_price
    cache_write_1h = inherited(
        "cache_creation_input_token_cost_above_1hr",
        base.cache_write_1h_per_mtok if base else None,
        floor=input_price,
    )
    return Price(
        input_per_mtok=input_price,
        output_per_mtok=output_price,
        cache_read_per_mtok=inherited(
            "cache_read_input_token_cost", base.cache_read_per_mtok if base else None
        ),
        cache_write_per_mtok=cache_write,
        cache_write_1h_per_mtok=cache_write
        if cache_write_1h is None
        else cache_write_1h,
        input_audio_per_mtok=inherited(
            "input_cost_per_audio_token", base.input_audio_per_mtok if base else None
        ),
        output_audio_per_mtok=inherited(
            "output_cost_per_audio_token", base.output_audio_per_mtok if base else None
        ),
    )
