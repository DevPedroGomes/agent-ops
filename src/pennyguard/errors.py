"""Exceptions raised by pennyguard.

The three refusals map to different HTTP answers, which is why they are
separate types rather than one exception with a reason string:

* ``BudgetExceeded``: a usage limit, resets at a known time. Answer 429 with
  ``Retry-After``.
* ``GuardKilled``: spending was stopped on purpose. Answer 503.
* ``GuardUnavailable``: the budget backend could not be read. Answer 503.
  An unreadable budget is not an absent budget, so the call is refused.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal


class PennyGuardError(Exception):
    """Base class for every refusal raised by a guard."""


class BudgetExceeded(PennyGuardError):
    """The reservation would take a budget past its limit."""

    def __init__(
        self,
        *,
        budget: str,
        scope: str | None,
        spent_usd: Decimal,
        limit_usd: Decimal,
        requested_usd: Decimal,
        resets_at: datetime | None,
        retry_after: int | None,
    ) -> None:
        self.budget = budget
        self.scope = scope
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        self.requested_usd = requested_usd
        self.resets_at = resets_at
        self.retry_after = retry_after
        target = f"{budget!r}" if scope is None else f"{budget!r} for {scope!r}"
        super().__init__(
            f"budget {target} exceeded: ${spent_usd} of ${limit_usd} spent, "
            f"${requested_usd} requested"
        )


class GuardKilled(PennyGuardError):
    """The kill switch is engaged for this project."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"spending is stopped: {reason}")


class GuardUnavailable(PennyGuardError):
    """The budget backend could not be read, so the call is refused."""


class UnknownModel(PennyGuardError, LookupError):
    """No price is known for this model."""

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"no price known for model {model!r}")
