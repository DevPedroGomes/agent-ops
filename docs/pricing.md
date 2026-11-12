# Pricing and usage

How pennyguard turns a response into dollars: where prices come from, how
each kind of token is priced, and how usage is read from each provider.

## Price tables

pennyguard ships no prices. A bundled table goes stale the week a provider
changes a price, and a stale price in a spending cap undercharges silently.

```python
from pennyguard import Price, Pricing

# Your own table, in dollars per million tokens.
pricing = Pricing({
    "claude-opus-5-5": Price(4, 20, cache_read_per_mtok="0.20"),
    "gpt-4o": Price(2.5, 10, cache_read_per_mtok=1.25),
})

# Or LiteLLM's community-maintained table, with your own entries on top.
pricing = Pricing.from_litellm("model_prices_and_context_window.json").with_prices({
    "my-fine-tune": Price(3, 12),
})
```

* **Versioned names** resolve to their base entry when the suffix is a date,
  a version or `latest`: `gpt-4o-2024-08-06` finds `gpt-4o`. A different model
  never matches by prefix: `gpt-4o-mini` does not fall back to `gpt-4o`.
* **Unknown models** raise `UnknownModel` from `Pricing.price`. At settle time
  the guard keeps the estimate instead and emits `pricing_failed`. Pass
  `default=Price(...)` to price unknown models conservatively instead.
* **`from_litellm`** keeps chat and completion models with token prices. It
  reads cache prices, one-hour cache writes, long-context tiers, audio prices
  and the `priority` and `flex` rates with their own tiers. A price an entry
  leaves out for a tier or variant is the standard one, scaled up by how much
  dearer that tier's input is. Batch rates are not read.

## How each token is priced

* **Cache prices default upward** when you leave them out: a cache read costs
  the input price, a cache write 1.25 times it, and a one-hour cache write
  (Anthropic's `ttl: "1h"`, reported under `usage.cache_creation`) twice it.
  Pass the real numbers to stop overcharging.
* **Long-context tiers** (`above={200_000: Price(...)}`) price the whole call at
  the tier once the prompt passes the threshold, cached tokens included.
* **Dearer rates** such as Anthropic's fast mode or OpenAI's priority tier are
  read from the response into `Usage.tier` and priced from
  `variants={"fast": Price(...)}`. A variant can carry its own `above` tiers.
  A dearer rate with no price, including a long prompt at a variant without
  a tier for it, is not billed at the standard price: settle keeps the
  estimate and emits `pricing_failed`. Flex is cheaper than standard, so where
  it has no price of its own it is charged at the standard one.
* **Audio tokens** (OpenAI's audio models, Gemini's audio input) are read
  into `Usage.input_audio_tokens` and `output_audio_tokens` and priced from
  `input_audio_per_mtok` and `output_audio_per_mtok`. Audio with no audio
  price is treated like a dearer rate with no price. Cached audio is priced
  as cached text, because responses do not report it apart.

## Estimates

`guard.estimate(model, input_tokens=..., max_output_tokens=...)` is an upper
bound for one call: every input token at the dearest of the input and
cache-write prices, and the full `max_output_tokens`, thinking included.

* Pass `tier="fast"` (or another variant) when the call runs at that rate,
  and `audio=True` when it sends or returns audio.
* With refusal fallbacks a declined call is billed twice, so add the fallback
  model's estimate if overshoot matters (the FastAPI example does).
* A good estimate matters: the limit is checked against it, and a call that
  costs more is still recorded in full.

## Reading usage from responses

`settle_response` and `Usage.all_from_response` read Anthropic Messages, OpenAI
Chat Completions and OpenAI Responses objects, or the same shapes as dicts.

* Anthropic reports uncached input, cache reads and cache writes separately.
  OpenAI includes cached tokens in `prompt_tokens` and repeats them under
  `prompt_tokens_details`. Both map to one `Usage` shape so nothing is counted
  twice and cache writes are not missed.
* OpenAI's audio tokens, which its totals include, are split out from text.
* **Anthropic refusal fallbacks.** With server-side `fallbacks`, a declined
  attempt and the fallback model's answer are billed separately, each at its
  own model's rate, and the top-level `usage` covers only the answer. Every
  attempt is listed in `usage.iterations`, and pennyguard prices each one
  under its own model. A declined attempt that produced no output is billed
  only for some refusal categories; pennyguard counts it regardless, which
  errs toward overcounting. Server-side compaction iterations are counted the
  same way. An attempt that names no model is priced under the response's
  model only when no other model ran; otherwise the estimate is kept.
* A response pennyguard cannot read keeps the estimate and emits
  `pricing_failed`, rather than turning a paid answer into an error.

## Not priced

* Batch discounts: a batch call is charged at the standard rate, which
  overcounts.
* Fees outside tokens, such as web search per query or code execution time.
  Settle with `cost_usd` if you use them.
