---
name: presentation
description: Build a slide deck from the user's knowledge base and recordings, and save it as a shareable artifact. Use whenever the user asks for a presentation, deck, or slides.
---

# presentation — building a deck

Goal: produce ONE deck the user can present, share as a link, or download — built from
what they already know (their knowledge base, their recordings), not from a web search,
unless they explicitly asked for outside sources.

A deck is a **hand-written HTML artifact you design from scratch**. You own the design:
layout, typography, color, illustration, motion, information density — all of it. There
is no template and no fill-in-the-blanks form; every deck is a custom-designed thing. The
point of this skill is to make that design GOOD, not to constrain it.

## The three questions, before any HTML

Every weak deck fails one of these, and no amount of visual polish rescues it. Answer all
three before you write a line of markup:

1. **What kind of deck is this?** A board update, a sales pitch, and a project kickoff
   have different obligations — different required slides, different order, different
   burden of proof. Read `skill://presentation/references/deck-types.md` and work from
   the matching outline. Don't invent a structure when a known-good one exists.
2. **Who is in the room, and what do they do next?** A deck exists to move a specific
   audience to a specific decision or action. Name it in one sentence for yourself
   ("get the exec team to approve two more engineers for Q3"). If you can't, the deck
   has no spine and will come out as a list of facts.
3. **What is the through-line?** One sentence that survives if every slide is forgotten.
   Every slide either advances it or gets cut.

## Reference images — check before you design

If the user has uploaded images to this artifact (a logo, a photo, a screenshot, a style
reference), **use them** — a real image beats anything you can draw:

```
artifacts_read(action='assets', artifact_id='a-xxxxxxxx')
```

This shows you the images, not just their names. Check it when revising an existing
artifact, and whenever the user mentions a logo/photo/screenshot they've provided. Place
them by the **relative path shown** (`<img src="assets/logo.png">`) and never invent a
path you haven't seen listed. Details in `references/visual-craft.md` §0.

For a brand-new deck there is no artifact yet, so there are no assets — save the deck
first, then the user can drop images onto it and ask for a revision.

## Source priority — hard order, never skip ahead

1. **Knowledge hub first.** `kb_read(action='search', query=...)` to find the relevant notes
   (and `kb_read(action='list')` to browse an area), then read them. This is almost always
   where a deck's content should come from — cite note paths on the Sources slide.
2. **Recordings second**, when the topic is a specific meeting/discussion or the
   knowledge base is thin: `recordings_read(action='list')` then `recordings_read(action='read', recording_id=...)`.
   Cite the recording (name + id) on the Sources slide.
3. **Web — ONLY if the user explicitly asked for it** ("pull the latest numbers from
   the web", "include what's publicly said about X"). Never reach for `web_search` /
   `web_fetch` as a silent fallback just because the KB/recordings came up thin. If
   local sources don't cover the topic and the user didn't ask for the web, say plainly
   what's missing and build the deck from what you do have — don't invent content and
   don't go looking outside without permission.

Content is sourced; **design is yours**. The priority order governs where facts come
from — it says nothing about how the deck should look. Never pad a deck with invented
content to fill a layout; shape the layout to the content you actually have.

**Never fabricate a number, a quote, a logo, or a customer name.** If a slide's form
wants a metric you don't have, change the form — an honest qualitative slide beats an
invented chart. Where a figure is the user's to supply, write a visible placeholder
(`[metric needed]`) rather than a plausible-looking fake, and list those gaps in your
reply.

## Reference files — read the ones you need

Pull these with `kb_read(action='read', path='skill://presentation/<file>')`:

- **`references/deck-types.md`** — the 12 common deck types, each with its required
  slide sequence, what every slide must cover, length, and the failure mode to avoid.
  **Read this for essentially every deck** — it is what makes the content right.
- **`references/visual-craft.md`** — how to make the deck LOOK professional: the layout
  system, type scale, color, and above all **how to build visuals that aren't thin line
  diagrams**. Read this whenever visual quality matters, which is most of the time.
- **`references/html-scaffold.md`** — the working structural pattern for the deck
  chrome (slide switching, keyboard nav, progress, speaker notes, print-to-PDF,
  light/dark). Read this when you want the plumbing solved so your effort goes into
  design and content.

## Building the deck

Design the deck as ONE self-contained HTML file and save it with
`artifacts(action='save', kind='presentation', title=..., content=<full html>)`.

Every deck needs this plumbing (see `references/html-scaffold.md` for a working pattern):
slide switching one-at-a-time with arrow/space/click nav, a progress indicator,
`@media print` pagination so the PDF download is clean, and `prefers-color-scheme`
theming that works in both modes — including your SVG fills.

**Design guidance — where your creativity should go:**

- **One idea per slide.** Length follows the deck type (see `deck-types.md`); 10–15 is
  the common range. Longer only if the user asked for depth.
- **Pick the form to fit the idea**, don't force everything into bullets. A single big
  number, a quote, a two-column compare, a diagram, a timeline, a full-bleed section
  divider — each earns its slide. **Bullets are the fallback, not the default**; a deck
  where every slide is a bulleted list is a failed deck.
- **Write headlines that assert, not label.** "Revenue up 40% on enterprise renewals"
  beats "Q3 Revenue". The headline should carry the point even if the audience reads
  nothing else on the slide.
- **Design a real system, not decoration.** Choose a palette and a type scale that fit
  the content's tone (a board review and a launch teaser should not look alike) and apply
  them consistently via CSS custom properties. Restraint reads as polish.
- **Make the visuals substantial.** This is the single most common quality failure:
  thin, sparse line drawings that look like placeholder wireframes. `visual-craft.md`
  is specifically about fixing that — read it.

**Hard constraints (these are about the artifact working, not about your design):**

- **One HTML file.** Inline all CSS/JS. No CDN links, no external stylesheets, no external
  fonts — a strict standalone viewer blocks external requests, so anything remote silently
  breaks. System font stacks only.
- **Images: uploaded assets by relative path, or inline SVG / data: URIs.** An asset the
  user uploaded to this artifact is referenced as `<img src="assets/logo.png">` — it is
  served from the artifact's own directory, so it works for anyone with the link. Anything
  else must be inline SVG or a data: URI. No remote image URLs (unless the user explicitly
  asked for a web image) and never a `file://` or arbitrary local path — the deck is
  viewed standalone and those resolve to nothing.
- **Last slide is always Sources** — note paths / recording names+ids, and URLs only if
  the web was actually used (per the source rule above).

## Delivery — least back-and-forth

- Proceed with sensible defaults (audience = the user, tone = concise/professional,
  length = whatever the content supports) and state the assumption in your reply
  instead of asking first. Only use `ask` — once, bundling every open question — if the
  TOPIC itself is genuinely ambiguous (e.g. "make a deck about the project" with
  multiple same-named projects in the KB).
- Before creating: `artifacts_read(action='list')` if this looks like a revision of something already
  made ("add a slide to my Atlas deck"), then `artifacts_read(action='read', artifact_id=...)` its id
  to get the current HTML and pass that id back to `artifacts(action='save', artifact_id=...)` to
  update in place rather than creating a duplicate — the share link stays the same. Edit
  the HTML you get back; don't rebuild from scratch when revising.
- Reply with the returned share link as a clickable markdown link, a one-line description
  of what's in the deck and what it's built from (KB / recordings / web), and — if any —
  the list of placeholders the user needs to fill in.
