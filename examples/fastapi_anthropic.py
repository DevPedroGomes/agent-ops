"""A FastAPI endpoint with per-tenant dollar limits on Claude calls.

    pip install "pennyguard[redis]" anthropic fastapi uvicorn
    uvicorn examples.fastapi_anthropic:app --workers 4

Every worker shares the limits through Redis. Requests carry the tenant in an
``X-Tenant-Id`` header to keep the example short; take it from your auth.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

import anthropic
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from redis.asyncio import Redis

from pennyguard import (
    AsyncRedisStore,
    Budget,
    BudgetExceeded,
    Guard,
    GuardKilled,
    GuardUnavailable,
    Price,
    Pricing,
)

MODEL = "claude-opus-5-5"
FALLBACK_WORST_CASE = "claude-opus-5"
# A hard ceiling on output (thinking included) is what bounds the estimate
# below, so it is deliberately modest here.
MAX_TOKENS = 2_048

# Dollars per million tokens, Anthropic list prices as of November 2026.
# Check https://www.anthropic.com/pricing before relying on them. The two
# fallback models are priced too: with server-side refusal fallback a
# declined request is answered by one of them, and both attempts are billed.
pricing = Pricing(
    {
        "claude-opus-5-5": Price(4, 20, cache_read_per_mtok="0.20"),
        "claude-opus-5": Price(5, 25),
        "claude-opus-4-8": Price(5, 25),
    }
)

redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379"))
guard = Guard(
    AsyncRedisStore(redis),
    [
        Budget("global", limit_usd=200, window="day"),
        Budget("tenant-daily", limit_usd=5, window="day", per="tenant"),
        Budget("tenant-monthly", limit_usd=60, window="month", per="tenant"),
    ],
    project="docs-qa",
    pricing=pricing,
)
client = anthropic.AsyncAnthropic()


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    yield
    await client.close()
    await redis.aclose()


app = FastAPI(lifespan=lifespan)


class Question(BaseModel):
    text: str


@app.post("/ask")
async def ask(question: Question, x_tenant_id: str = Header()) -> dict[str, str]:
    # Characters / 3 overestimates English token counts. For an exact number,
    # call client.messages.count_tokens first. A declined request is billed
    # for the attempt that declined and for the fallback that answered, so
    # the worst case is both. Drop the second term to reserve less and accept
    # a small overshoot on the rare declined call.
    input_tokens = len(question.text) // 3 + 50
    estimate = sum(
        guard.estimate(m, input_tokens=input_tokens, max_output_tokens=MAX_TOKENS)
        for m in (MODEL, FALLBACK_WORST_CASE)
    )
    try:
        response = await guard.call(
            lambda: client.beta.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": question.text}],
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ),
            estimate_usd=estimate,
            tenant=x_tenant_id,
            tags={"route": "ask"},
        )
    except BudgetExceeded as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(429, "Daily usage limit reached", headers=headers) from exc
    except (GuardKilled, GuardUnavailable) as exc:
        raise HTTPException(503, "Temporarily unavailable") from exc

    if response.stop_reason == "refusal":
        raise HTTPException(422, "The model declined this request")
    text = "".join(block.text for block in response.content if block.type == "text")
    return {"answer": text}


@app.get("/usage")
async def usage(x_tenant_id: str = Header()) -> list[dict[str, str]]:
    return [
        {
            "budget": s.budget,
            "spent_usd": str(s.spent_usd),
            "remaining_usd": str(s.remaining_usd),
            "resets_at": s.resets_at.isoformat() if s.resets_at else "never",
        }
        for s in await guard.status(tenant=x_tenant_id)
    ]
