"""Token usage, normalized across providers.

Providers report input tokens differently. Anthropic's ``input_tokens``
excludes cached tokens, which come separately as
``cache_read_input_tokens`` and ``cache_creation_input_tokens``. OpenAI's
``prompt_tokens`` (Chat Completions) and ``input_tokens`` (Responses)
include the cached part, reported again under ``*_tokens_details``. Adding
the raw fields together would bill cached tokens twice on one provider and
miss cache writes on the other, so everything is mapped to one shape here.

The readers duck-type the response: they accept the SDK objects and plain
dicts, and import no provider SDK.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _count(obj: Any, name: str) -> int:
    value = _get(obj, name)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        # ValueError, not TypeError: a malformed response is bad data, and
        # settle_response treats every ValueError from here as "keep the
        # estimate" rather than crash after the call was paid for.
        raise ValueError(  # noqa: TRY004
            f"usage field {name!r} is not an integer: {value!r}"
        )
    return value


def _anthropic_tier(usage: Any) -> str | None:
    # Fast mode bills at a multiple of the standard rate and reports itself in
    # usage.speed. Anything else is the standard rate.
    return "fast" if _get(usage, "speed") == "fast" else None


def _model(response: Any) -> str:
    model = _get(response, "model")
    if not isinstance(model, str) or not model:
        raise ValueError("response has no model name")
    return model


@dataclass(frozen=True)
class Usage:
    """Tokens billed for one call.

    ``input_tokens`` counts only uncached input, billed at the full input
    price. Cache reads and cache writes are separate because they are priced
    separately.

    ``tier`` names a non-standard rate the call ran at: ``"fast"`` for
    Anthropic's fast mode, ``"priority"`` or ``"flex"`` for OpenAI's service
    tiers. ``None`` means the standard rate.
    """

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    """Cache writes at the default (five-minute) rate."""
    tier: str | None = None
    cache_write_1h_tokens: int = 0
    """Cache writes with a one-hour TTL, billed at a higher rate."""

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("model must not be empty")
        if self.tier is not None and not self.tier:
            raise ValueError("tier must be None or a non-empty string")
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cache_write_1h_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int, got {value!r}")

    @property
    def prompt_tokens(self) -> int:
        """Every input token, cached or not. Long-context price tiers use this."""
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.cache_write_1h_tokens
        )

    @classmethod
    def from_anthropic(cls, response: Any) -> Usage:
        """Read the top-level usage of a Messages API response.

        That is the attempt that produced the returned message only. A
        response served by a refusal fallback, or one that ran server-side
        compaction, also bills the attempts listed in ``usage.iterations``;
        ``all_from_response`` reads those.
        """
        usage = _get(response, "usage")
        if usage is None:
            raise ValueError("response has no usage")
        return cls._anthropic_entry(usage, _model(response), _anthropic_tier(usage))

    @classmethod
    def _anthropic_entry(cls, entry: Any, model: str, tier: str | None) -> Usage:
        # cache_creation_input_tokens is every cache write; the cache_creation
        # breakdown says how many had a one-hour TTL, which costs more.
        writes = _count(entry, "cache_creation_input_tokens")
        breakdown = _get(entry, "cache_creation")
        long_writes = (
            _count(breakdown, "ephemeral_1h_input_tokens")
            if breakdown is not None
            else 0
        )
        if long_writes > writes:
            raise ValueError(
                f"ephemeral_1h_input_tokens {long_writes} exceeds "
                f"cache_creation_input_tokens {writes}"
            )
        return cls(
            model=model,
            input_tokens=_count(entry, "input_tokens"),
            output_tokens=_count(entry, "output_tokens"),
            cache_read_tokens=_count(entry, "cache_read_input_tokens"),
            cache_write_tokens=writes - long_writes,
            tier=tier,
            cache_write_1h_tokens=long_writes,
        )

    @classmethod
    def all_from_response(cls, response: Any) -> list[Usage]:
        """Every billed attempt in a response, each under its own model.

        With Anthropic's server-side refusal fallback, a declined attempt and
        the fallback that answered are separate entries in
        ``usage.iterations``, billed at their own models' rates, while the
        top-level usage covers only the attempt that answered. Reading the
        top level alone undercounts. A declined attempt that produced no
        output is billed only for some refusal categories; it is counted
        here regardless, which errs toward overcounting.
        """
        usage = _get(response, "usage")
        if usage is None:
            raise ValueError("response has no usage")
        iterations = _get(usage, "iterations")
        if iterations and _get(usage, "prompt_tokens") is None:
            model = _model(response)
            tier = _anthropic_tier(usage)
            return [
                cls._anthropic_entry(entry, _get(entry, "model") or model, tier)
                for entry in iterations
            ]
        return [cls.from_response(response)]

    @classmethod
    def from_openai(cls, response: Any) -> Usage:
        """Read an OpenAI Chat Completions or Responses API response.

        Also reads any OpenAI-compatible response, such as LiteLLM's.
        """
        usage = _get(response, "usage")
        if usage is None:
            raise ValueError("response has no usage")
        if _get(usage, "prompt_tokens") is not None:
            prompt = _count(usage, "prompt_tokens")
            output = _count(usage, "completion_tokens")
            details = _get(usage, "prompt_tokens_details")
        else:
            prompt = _count(usage, "input_tokens")
            output = _count(usage, "output_tokens")
            details = _get(usage, "input_tokens_details")
        cached = _count(details, "cached_tokens") if details is not None else 0
        if cached > prompt:
            raise ValueError(f"cached_tokens {cached} exceeds input tokens {prompt}")
        service_tier = _get(response, "service_tier")
        return cls(
            model=_model(response),
            input_tokens=prompt - cached,
            output_tokens=output,
            cache_read_tokens=cached,
            tier=service_tier if service_tier in ("priority", "flex") else None,
        )

    @classmethod
    def from_response(cls, response: Any) -> Usage:
        """Pick the reader from the shape of ``response.usage``.

        ``prompt_tokens`` or ``input_tokens_details`` means OpenAI; anything
        else with ``input_tokens`` is read as Anthropic.
        """
        usage = _get(response, "usage")
        if usage is None:
            raise ValueError("response has no usage")
        if (
            _get(usage, "prompt_tokens") is not None
            or _get(usage, "input_tokens_details") is not None
        ):
            return cls.from_openai(response)
        if _get(usage, "input_tokens") is not None:
            return cls.from_anthropic(response)
        raise ValueError("unrecognized usage shape")
