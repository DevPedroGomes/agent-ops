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
    if obj is None:
        return None
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


def _entry_model(entry: Any) -> str | None:
    model = _get(entry, "model")
    return model if isinstance(model, str) and model else None


def _model(response: Any) -> str:
    model = _get(response, "model")
    if not isinstance(model, str) or not model:
        raise ValueError("response has no model name")
    return model


@dataclass(frozen=True)
class Usage:
    """Tokens billed for one call.

    ``input_tokens`` counts only uncached text input, billed at the full
    input price. Cache reads, cache writes and audio are separate because
    they are priced separately.

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
    input_audio_tokens: int = 0
    """Audio input, billed at the model's audio rate."""
    output_audio_tokens: int = 0
    """Audio output, billed at the model's audio rate."""

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
            "input_audio_tokens",
            "output_audio_tokens",
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
            + self.input_audio_tokens
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

        An attempt that names no model is priced under the response's model
        when only that model ran (compaction, say). When a fallback ran too,
        such an attempt could have run on either, so this raises
        ``ValueError`` and the guard keeps the estimate.
        """
        usage = _get(response, "usage")
        if usage is None:
            raise ValueError("response has no usage")
        iterations = _get(usage, "iterations")
        if not iterations or _get(usage, "prompt_tokens") is not None:
            return [cls.from_response(response)]
        if not isinstance(iterations, list | tuple):
            # ValueError like every other malformed field: see _count.
            raise ValueError(  # noqa: TRY004
                f"usage.iterations is not a list: {iterations!r}"
            )
        model = _model(response)
        tier = _anthropic_tier(usage)
        named = {_entry_model(entry) for entry in iterations} - {None}
        several = bool(named - {model}) or any(
            _get(entry, "type") == "fallback_message" for entry in iterations
        )
        result = []
        for entry in iterations:
            ran_on = _entry_model(entry)
            if ran_on is None:
                if several:
                    raise ValueError(
                        "an attempt in usage.iterations names no model while "
                        "more than one model ran, so it cannot be priced"
                    )
                ran_on = model
            result.append(cls._anthropic_entry(entry, ran_on, tier))
        return result

    @classmethod
    def from_openai(cls, response: Any) -> Usage:
        """Read an OpenAI Chat Completions or Responses API response.

        Also reads any OpenAI-compatible response, such as LiteLLM's. Audio
        tokens, which the totals include, are split out under
        ``input_audio_tokens`` and ``output_audio_tokens``.
        """
        usage = _get(response, "usage")
        if usage is None:
            raise ValueError("response has no usage")
        if _get(usage, "prompt_tokens") is not None:
            prompt = _count(usage, "prompt_tokens")
            output = _count(usage, "completion_tokens")
            details = _get(usage, "prompt_tokens_details")
            output_details = _get(usage, "completion_tokens_details")
        else:
            prompt = _count(usage, "input_tokens")
            output = _count(usage, "output_tokens")
            details = _get(usage, "input_tokens_details")
            output_details = _get(usage, "output_tokens_details")
        cached = _count(details, "cached_tokens")
        audio_in = _count(details, "audio_tokens")
        audio_out = _count(output_details, "audio_tokens")
        if cached + audio_in > prompt:
            raise ValueError(
                f"cached_tokens {cached} and audio_tokens {audio_in} exceed "
                f"input tokens {prompt}"
            )
        if audio_out > output:
            raise ValueError(f"audio_tokens {audio_out} exceed output tokens {output}")
        service_tier = _get(response, "service_tier")
        return cls(
            model=_model(response),
            input_tokens=prompt - cached - audio_in,
            output_tokens=output - audio_out,
            cache_read_tokens=cached,
            tier=service_tier if service_tier in ("priority", "flex") else None,
            input_audio_tokens=audio_in,
            output_audio_tokens=audio_out,
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
