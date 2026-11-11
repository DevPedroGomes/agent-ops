"""The public surface, sync/async parity, OpenTelemetry and the examples."""

from __future__ import annotations

import inspect
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

import pennyguard
from pennyguard import (
    Budget,
    BudgetExceeded,
    Event,
    Guard,
    MemoryStore,
    Reservation,
    SyncGuard,
    SyncReservation,
    Usage,
)
from tests.conftest import Clock

ROOT = Path(__file__).resolve().parents[1]


def test_public_names() -> None:
    assert set(pennyguard.__all__) == {
        "AsyncRedisStore",
        "Budget",
        "BudgetExceeded",
        "BudgetStatus",
        "Event",
        "Guard",
        "GuardKilled",
        "GuardUnavailable",
        "MemoryStore",
        "PennyGuardError",
        "Price",
        "Pricing",
        "RedisStore",
        "Reservation",
        "SyncGuard",
        "SyncReservation",
        "UnknownModel",
        "Usage",
        "Window",
        "__version__",
    }
    for name in pennyguard.__all__:
        assert hasattr(pennyguard, name)


def test_importing_opens_nothing_and_needs_no_extras() -> None:
    code = (
        "import sys, pennyguard; "
        "assert 'redis' not in sys.modules, 'redis imported'; "
        "assert 'opentelemetry' not in sys.modules, 'otel imported'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def _public(cls: type) -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(member)
        for name, member in vars(cls).items()
        if not name.startswith("_") and callable(member)
    }


@pytest.mark.parametrize(
    ("async_cls", "sync_cls"), [(Guard, SyncGuard), (Reservation, SyncReservation)]
)
def test_sync_and_async_have_the_same_api(async_cls: type, sync_cls: type) -> None:
    async_api, sync_api = _public(async_cls), _public(sync_cls)
    assert async_api.keys() == sync_api.keys()
    for name, signature in async_api.items():
        a = [(p.name, p.kind, p.default) for p in signature.parameters.values()]
        s = [(p.name, p.kind, p.default) for p in sync_api[name].parameters.values()]
        assert a == s, name


def test_otel_metrics(store: MemoryStore, clock: Clock) -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from pennyguard import Price, Pricing
    from pennyguard.otel import metrics_hook

    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    guard = SyncGuard(
        store,
        [Budget("tenant", 1, per="tenant")],
        project="otel",
        clock=clock,
        pricing=Pricing({"m": Price(1, 5)}),
        on_event=metrics_hook(meter, scope_keys=["tenant"], tag_keys=["feature"]),
    )
    guard.reserve("0.5", tenant="acme", tags={"feature": "chat"}).settle(
        usage=Usage("m", input_tokens=100_000, output_tokens=10_000)
    )
    with pytest.raises(BudgetExceeded):
        guard.reserve(1, tenant="acme")

    data = reader.get_metrics_data()
    assert data is not None
    points: dict[str, list[tuple[dict[str, str], float]]] = {}
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                points[metric.name] = [
                    (dict(p.attributes or {}), p.value) for p in metric.data.data_points
                ]

    cost = points["pennyguard.cost"]
    assert cost == [
        (
            {
                "project": "otel",
                "model": "m",
                "scope.tenant": "acme",
                "tag.feature": "chat",
            },
            pytest.approx(0.15),
        )
    ]
    tokens = {
        attrs["token.type"]: value for attrs, value in points["pennyguard.tokens"]
    }
    assert tokens == {"input": 100_000, "output": 10_000}
    assert points["pennyguard.refusals"] == [
        ({"project": "otel", "scope.tenant": "acme", "reason": "tenant"}, 1)
    ]


def test_event_amounts(store: MemoryStore, clock: Clock) -> None:
    events: list[Event] = []
    guard = SyncGuard(store, [Budget("g", 10)], clock=clock, on_event=events.append)
    guard.reserve(2).settle(1)
    assert [(e.kind, e.amount_usd) for e in events] == [
        ("reserved", Decimal(2)),
        ("settled", Decimal(1)),
    ]
    assert events[0].reservation_id == events[1].reservation_id


def test_quickstart_example_runs() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "quickstart.py")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "refused" in result.stdout
