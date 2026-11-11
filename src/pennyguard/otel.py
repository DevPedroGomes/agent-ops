"""OpenTelemetry metrics from guard events. Needs ``pennyguard[otel]``.

    from pennyguard.otel import metrics_hook
    guard = Guard(store, budgets, on_event=metrics_hook())

Instruments, all counters:

* ``pennyguard.cost`` (USD): settled cost, plus estimates kept by
  ``unsettled`` and ``pricing_failed`` reservations.
* ``pennyguard.tokens``: settled tokens, by ``token.type``.
* ``pennyguard.refusals``: refused reservations, by ``reason``
  (a budget name, ``killed`` or ``unavailable``).

Attributes are low-cardinality by default: ``project`` and ``model``. Scope
and tag keys are added only when listed in ``scope_keys`` and ``tag_keys``.
A per-tenant attribute is fine for a few hundred tenants and expensive for a
million users, which is why it is opt-in.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from pennyguard._core import Event


def metrics_hook(
    meter: Any = None,
    *,
    scope_keys: Iterable[str] = (),
    tag_keys: Iterable[str] = (),
) -> Callable[[Event], None]:
    """An ``on_event`` callback that records metrics on ``meter``.

    Without a meter, one named ``pennyguard`` is taken from the global meter
    provider.
    """
    try:
        from opentelemetry import metrics
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise ImportError(
            "metrics_hook needs opentelemetry-api: pip install 'pennyguard[otel]'"
        ) from exc

    meter = meter or metrics.get_meter("pennyguard")
    cost = meter.create_counter(
        "pennyguard.cost", unit="USD", description="Dollars spent on LLM calls"
    )
    tokens = meter.create_counter(
        "pennyguard.tokens", unit="{token}", description="Tokens billed"
    )
    refusals = meter.create_counter(
        "pennyguard.refusals", description="Reservations refused"
    )
    scope_keys = tuple(scope_keys)
    tag_keys = tuple(tag_keys)

    def attributes(event: Event) -> dict[str, str]:
        attrs = {"project": event.project}
        for key in scope_keys:
            if key in event.scope:
                attrs[f"scope.{key}"] = event.scope[key]
        for key in tag_keys:
            if key in event.tags:
                attrs[f"tag.{key}"] = event.tags[key]
        return attrs

    def hook(event: Event) -> None:
        attrs = attributes(event)
        if event.kind == "settled" and event.usage_costs:
            # One data point per billed attempt, under the model that ran it.
            for usage, amount in zip(event.usage, event.usage_costs, strict=True):
                with_model = {**attrs, "model": usage.model}
                cost.add(float(amount), with_model)
                for kind, count in (
                    ("input", usage.input_tokens),
                    ("output", usage.output_tokens),
                    ("cache_read", usage.cache_read_tokens),
                    ("cache_write", usage.cache_write_tokens),
                    ("cache_write_1h", usage.cache_write_1h_tokens),
                ):
                    if count:
                        tokens.add(count, {**with_model, "token.type": kind})
        elif event.kind in ("settled", "unsettled", "pricing_failed"):
            if event.amount_usd is not None:
                cost.add(float(event.amount_usd), attrs)
        elif event.kind == "refused":
            refusals.add(1, {**attrs, "reason": event.budget or "budget"})
        elif event.kind in ("killed", "unavailable"):
            refusals.add(1, {**attrs, "reason": event.kind})

    return hook
