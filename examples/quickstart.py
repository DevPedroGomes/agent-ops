"""pennyguard in two minutes, offline. Run: python examples/quickstart.py

MemoryStore keeps counters in this process. In production, swap it for
RedisStore (or AsyncRedisStore with Guard) and every worker shares the limits.
The model responses below are fakes shaped like the Anthropic SDK's.
"""

from types import SimpleNamespace

from pennyguard import (
    Budget,
    BudgetExceeded,
    GuardKilled,
    MemoryStore,
    Price,
    Pricing,
    SyncGuard,
)

# Illustrative prices in dollars per million tokens. Use your provider's
# current rate card, or Pricing.from_litellm(...) for a maintained table.
pricing = Pricing({"demo-model": Price(input_per_mtok=3, output_per_mtok=15)})

guard = SyncGuard(
    MemoryStore(),
    [
        Budget("global", limit_usd=5.00, window="day"),
        Budget("tenant", limit_usd=0.05, window="day", per="tenant"),
    ],
    project="quickstart",
    pricing=pricing,
)


def fake_model_call(output_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        model="demo-model",
        usage=SimpleNamespace(input_tokens=1_200, output_tokens=output_tokens),
    )


# The worst case for this call: 1,200 input tokens, at most 1,000 output.
estimate = guard.estimate("demo-model", input_tokens=1_200, max_output_tokens=1_000)
print(f"estimate per call: ${estimate}")

for attempt in range(1, 6):
    try:
        response = guard.call(
            lambda: fake_model_call(output_tokens=800),
            estimate_usd=estimate,
            tenant="acme",
        )
    except BudgetExceeded as exc:
        print(f"call {attempt}: refused, {exc} (retry in {exc.retry_after}s)")
        break
    print(f"call {attempt}: ok")

# Another tenant has its own allowance; the global budget is shared.
guard.call(lambda: fake_model_call(400), estimate_usd=estimate, tenant="globex")

for status in guard.status(tenant="acme"):
    who = f" for {status.scope}" if status.scope else ""
    print(
        f"{status.budget}{who}: ${status.spent_usd} of ${status.limit_usd}, "
        f"resets {status.resets_at:%Y-%m-%d %H:%M} UTC"
    )

# The kill switch stops every reservation in the project at once.
guard.kill("cost spike under investigation")
try:
    guard.reserve(estimate, tenant="globex")
except GuardKilled as exc:
    print(f"refused while killed: {exc.reason}")
guard.resume()
