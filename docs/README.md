# Curry Leaves — documentation

Everything written about Curry Leaves, grouped by what you're trying to do. Three kinds of doc,
each with a distinct job — start with the one that matches your question.

## User guide — [`guide/`](guide/README.md) — the end-user manual (no code)

How to *use* Curry Leaves: getting started, every screen, an FAQ, and a tour of what changes in
your day. It's a single self-contained web page — read it online at
**[ilayanambi.com/curryleaves](https://ilayanambi.com/curryleaves)** (source:
[`guide/site/index.html`](guide/site/index.html)) — and is the one source of truth for using the app.

## Architecture — [`architecture.md`](architecture.md)

The authoritative end-to-end map of the whole app: layering, the Work Kernel, the shared
transport, disk layout, and use-case walk-throughs. Read this first if you're new to the codebase.

## Design docs — [`design/`](design/) — deep mechanics, one subsystem each

The "go deeper" targets for the architecture doc.

- [work-kernel.md](design/work-kernel.md) — the run substrate: lanes, bands, autonomy, suspend/resume.
- [knowledge-base-design.md](design/knowledge-base-design.md) — the `cl_memory` bundle and tiered search.
- [dashboard-structured-tiles-design.md](design/dashboard-structured-tiles-design.md) — checked tile outputs.
- [artifact-store-design.md](design/artifact-store-design.md) — the artifact store and capability links.
- [tracing.md](design/tracing.md) — the span model behind Trace.
- [oauth-provider-registration.md](design/oauth-provider-registration.md) — the provider OAuth flows.

---

### How the tiers relate

```
CLAUDE.md / root README
        │
        ├──────────►  guide/   (how to USE it — one web page, no code)
        ▼
architecture.md  (whole app)
        │
        ▼
design/  (deepest subsystem mechanics)
```

Reading flows downward; each layer links to the next. Nothing here is orphaned — see
[architecture.md](architecture.md) for the map that ties the code to all of it.
