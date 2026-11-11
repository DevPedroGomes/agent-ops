from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pennyguard import Budget
from pennyguard.budget import counter_key, kill_key, seconds_until, window_bounds
from pennyguard.money import from_micros, to_decimal, to_micros


class TestMoney:
    def test_float_goes_through_repr(self) -> None:
        assert to_decimal(0.1) == Decimal("0.1")

    def test_accepts_int_str_decimal(self) -> None:
        assert to_decimal(2) == Decimal(2)
        assert to_decimal("0.25") == Decimal("0.25")
        assert to_decimal(Decimal("1.5")) == Decimal("1.5")

    def test_rejects_bool_and_garbage(self) -> None:
        with pytest.raises(TypeError):
            to_decimal(True)
        with pytest.raises(TypeError):
            to_decimal(None)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            to_decimal("abc")
        with pytest.raises(ValueError):
            to_decimal(float("nan"))

    def test_charges_round_up_limits_round_down(self) -> None:
        tiny = Decimal("0.0000001")
        assert to_micros(tiny) == 1
        assert to_micros(tiny, round_up=False) == 0
        assert to_micros(Decimal("1.5")) == 1_500_000
        assert from_micros(1_500_000) == Decimal("1.5")


class TestBudget:
    def test_valid(self) -> None:
        b = Budget("tenant-daily", 2.5, "day", per="tenant")
        assert b.limit_usd == Decimal("2.5")
        assert b.limit_micros == 2_500_000
        assert b.ttl_seconds == 2 * 86_400

    @pytest.mark.parametrize("name", ["", "has space", "a:b", "x" * 65, "{x}"])
    def test_rejects_bad_names(self, name: str) -> None:
        with pytest.raises(ValueError):
            Budget(name, 1)

    def test_rejects_bad_window_and_limit(self) -> None:
        with pytest.raises(ValueError):
            Budget("b", 1, "week")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            Budget("b", 0)
        with pytest.raises(ValueError):
            Budget("b", -1)

    @pytest.mark.parametrize("per", ["Tenant", "1x", "a-b", "tags", "estimate_usd"])
    def test_rejects_bad_dimensions(self, per: str) -> None:
        with pytest.raises(ValueError):
            Budget("b", 1, per=per)

    def test_is_hashable_and_comparable(self) -> None:
        assert Budget("b", 1) == Budget("b", "1.0")
        assert len({Budget("b", 1), Budget("b", 1)}) == 1


class TestWindows:
    now = datetime(2026, 12, 31, 23, 59, 30, tzinfo=UTC)

    def test_hour(self) -> None:
        period, end = window_bounds("hour", self.now)
        assert period == "20261231T23"
        assert end == datetime(2027, 1, 1, tzinfo=UTC)

    def test_day(self) -> None:
        period, end = window_bounds("day", self.now)
        assert period == "20261231"
        assert end == datetime(2027, 1, 1, tzinfo=UTC)

    def test_month_rolls_over_the_year(self) -> None:
        period, end = window_bounds("month", self.now)
        assert period == "202612"
        assert end == datetime(2027, 1, 1, tzinfo=UTC)
        assert window_bounds("month", datetime(2026, 2, 10, tzinfo=UTC))[1] == (
            datetime(2026, 3, 1, tzinfo=UTC)
        )

    def test_total_never_resets(self) -> None:
        assert window_bounds("total", self.now) == ("all", None)

    def test_converts_other_time_zones_to_utc(self) -> None:
        from datetime import timedelta, timezone

        local = datetime(2026, 11, 11, 22, 0, tzinfo=timezone(timedelta(hours=-3)))
        assert window_bounds("day", local)[0] == "20261112"

    def test_seconds_until_rounds_up_with_a_floor_of_one(self) -> None:
        end = datetime(2027, 1, 1, tzinfo=UTC)
        assert seconds_until(end, self.now) == 30
        assert seconds_until(end, datetime(2026, 12, 31, 23, 59, 59, 600_000, UTC)) == 1
        assert seconds_until(end, end) == 1
        assert seconds_until(None, self.now) is None


class TestKeys:
    def test_scope_values_cannot_collide(self) -> None:
        budget = Budget("b", 1, per="tenant")
        a = counter_key("p", budget, "20261111", "a:b")
        b = counter_key("p", budget, "20261111", "a%3Ab")
        assert a != b
        assert a.count(":") == b.count(":") == 4

    def test_project_is_a_cluster_hash_tag(self) -> None:
        budget = Budget("b", 1)
        assert counter_key("my app", budget, "x", None).startswith("pg:{my%20app}:")
        assert kill_key("{evil}") == "pg:{%7Bevil%7D}:kill"
