---
name: web-search
description: How to answer a question from the open web — which tool to reach for, how to query effectively, how to fall back when a tool comes back empty, and how to use the browser as a last resort. Load this before answering anything about live or public information (weather, prices, scores, news, who/what/when, a fact you can't already know) rather than guessing or saying you can't look it up.
hide: true
---

# Answer from the web

Three tools, in escalation order. Start at the top; only step down a rung when the one above
came back thin or empty. Never skip straight to the browser — it's the slow, heavy path.

| Tool | What it does | Reach for it when |
|---|---|---|
| `web_search` | DuckDuckGo → top results as title, URL, snippet | ALWAYS first. The snippet alone often answers the question. |
| `web_fetch` | Fetch ONE page → clean main content as markdown (auto-renders JS) | The snippet wasn't enough and you need to READ a specific result. |
| `browser` | Headless browser: click, fill, paginate, screenshot | Only when a plain read fails — the page needs interaction, login, pagination, or the content lives behind a click. |

## The loop

1. **Search first.** `web_search(query=…)`. Read the snippets — for a weather, score, price,
   time, or "who won" question, the answer is usually right there. If it is, **answer and
   stop.** Don't fetch a page you don't need.
2. **Fetch when you need the detail.** If no snippet answers it, `web_fetch(url=…)` the single
   most relevant result and read its content. Prefer the primary source (the official site, the
   org, the doc) over an aggregator when both appear.
3. **Escalate to the browser only if fetch fails.** If `web_fetch` returns an error, "No
   readable content", or a page that clearly needs interaction (a search box, "load more", a
   login, an infinite scroll), use `browser`: `goto` the URL, then `text` to read it, `click`/
   `fill` to get past what's blocking the content.

## Search effectively

A better query beats a second tool. Before searching:

- **Use the specific words that will appear on the answer page**, not the user's phrasing.
  "how cold is it in Chennai" → search `Chennai weather today`. "did the match finish" →
  `<team> vs <team> score`.
- **Keep it short — 2 to 5 keywords.** Search engines match terms, not sentences. Drop filler
  ("what is the", "can you tell me", "right now").
- **Add a qualifier when the bare query is ambiguous**: a place (`… near Austin`), a year
  (`… 2026`), a unit, or a site (`site:gov.uk …`) to pin down the source.
- **Names and entities exact.** Quote a multi-word name or title (`"Acme Series B"`) so it
  isn't split across unrelated results.

## When a tool comes back empty — switch, don't stall

Coming back with nothing is a failure of approach, not a dead end. In order:

1. **Reword and search again.** Different keywords, a synonym, a broader or narrower angle. Most
   empty results are a wording problem — one retry with better terms usually lands it. (One
   reword, not ten; two good searches, then move on.)
2. **Fetch a result you skipped.** A snippet can look irrelevant while the page answers it.
   `web_fetch` the next-best URL.
3. **Go to the primary source directly.** If you know where the answer lives (a `.gov` site, an
   official scoreboard, a company's own page), `web_fetch` — or `browser` `goto` — that URL
   instead of searching for it.
4. **Browser as the last resort.** If the content is rendered only after interaction, drive it:
   `goto`, then `text`; `fill` a search field and `click` submit; `click` "load more" or
   "next" to paginate. Read the result with `text`.

Only after all of that comes back empty do you say you couldn't find it — and then say so
plainly, in one sentence, rather than guessing.

## Don't

- Don't **speculate** when a search would settle it. If it's checkable, check it.
- Don't **fetch pages you don't need** — if the snippet answered it, you're done.
- Don't **loop forever**: two reworded searches, then a fetch, then the browser. If the browser
  can't get it either, stop and say so.
- Don't **trust one weak source** for a number that matters — if two results disagree on a
  price or a figure, say which you're going with and that sources differ.
