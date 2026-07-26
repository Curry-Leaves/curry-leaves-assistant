# visual-craft — making a deck look professionally designed

Visuals come from two places: **real images the user uploaded** to the artifact (check
`artifacts_read(action='assets', artifact_id=...)` — see §0), and **inline SVG you draw by
hand** for everything else. Drawing by hand is a real constraint, but it is NOT the reason
decks come out looking cheap. Thin, sparse, wireframe-looking line diagrams are a *craft*
failure, not a medium failure — inline SVG is the same medium that produces high-end
editorial and data-journalism graphics.

This file is the fix. The default failure mode to design against:

> Three 1px-stroke rectangles connected by 1px arrows, all in the same grey, floating in
> the middle of an empty slide with a caption underneath.

That reads as a placeholder. Everything below is how to not produce it.

---

## 0. Use the real images first

Before designing any slide that wants a photo, a logo, a product shot, or a screenshot,
check what the user has actually given you:

```
artifacts_read(action='assets', artifact_id='a-xxxxxxxx')
```

That returns the file list **and shows you the images themselves**, so you can design
around what they actually contain — match the deck's palette to the photo, crop and place
it deliberately, decide which slide it belongs on.

- Place them by **relative path**: `<img src="assets/hero.jpg">`. The image is served from
  the artifact's own directory, so this works for anyone who opens the share link.
- Never a data URI (it bloats the file for no gain), never an absolute URL, and **never a
  path you didn't see in the manifest** — an invented filename is a broken image.
- Style them like designed elements, not dropped-in rectangles: `object-fit: cover` in a
  shaped container, full-bleed where the composition supports it, a consistent corner
  radius, and a scrim/gradient overlay when text sits on top.
- Assets aren't only for photos. A supplied logo, chart screenshot, or product shot is
  always better than your redrawing of it.

If there are no assets, everything below applies. If a slide really needs a photo you
don't have, say so in your reply rather than faking it — the user can drop one onto the
artifact and you can place it on the next pass.

---

## 1. The seven rules that separate real graphics from wireframes

Apply these to EVERY visual you draw. Most "bad AI diagram" symptoms are one of these
missing.

**1. Fill, don't outline.** The single highest-impact change. A shape defined by a solid
or tinted *fill* reads as designed; a shape defined only by a 1px stroke reads as
unfinished. Default to filled shapes with no stroke, or a fill plus a deliberately heavy
stroke (2–3px) in a darker shade of the same hue. Never a lone hairline outline.

**2. Commit to scale.** A visual should occupy the space it's given — typically 55–70% of
the slide area. A small diagram centered in a large empty slide looks like a mistake.
Scale the artwork up until it has presence; let it bleed off the slide edge when the
composition supports it. Full-bleed is a legitimate and underused choice.

**3. Build depth in layers.** Flat single-layer art looks thin. Compose each visual from
3–5 stacked layers: a background field (a large soft shape, a gradient wash, a tint
block), a mid layer (the primary forms), a detail layer (accents, ticks, small marks),
and a label layer on top. Depth comes from overlap and from opacity variation, not from
drop shadows.

**4. Use weight contrast.** Within one graphic, deliberately vary stroke weights and fill
densities — a dominant element at full saturation, supporting elements at 30–50% opacity,
context elements at 10–15%. Uniform weight everywhere is what makes a diagram read as
undifferentiated wireframe. Hierarchy in a graphic works exactly like hierarchy in type.

**5. Round, chamfer, or otherwise shape the geometry.** Default rectangles with square
corners are the visual signature of a wireframe. Use generous corner radii (8–20px at
slide scale), circles, arcs, angled cuts, or organic paths. Consistency in that choice is
what makes it read as a system.

**6. Two hues maximum, many values.** Pick your accent hue and one neutral, then generate
5–7 *values* of each (via `color-mix()` or explicit tints). Rich-looking graphics almost
always come from many values of few hues. Many hues at similar value is what makes a
diagram look like a default chart library.

**7. Add texture — sparingly.** One subtle gradient, a dot/line pattern fill in a large
shape, or a soft blurred blob behind the composition. This is the difference between
"drawn in a diagram tool" and "designed". One texture per visual; more becomes noise.

---

## 2. The SVG techniques you are underusing

These are all inline-SVG native, self-contained, and theme-safe. Reach for them.

**Gradients** — define once in `<defs>`, reuse everywhere. A two-stop linear gradient in
one hue instantly reads richer than a flat fill.

```svg
<defs>
  <linearGradient id="g1" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%"  stop-color="var(--accent)" stop-opacity="0.95"/>
    <stop offset="100%" stop-color="var(--accent)" stop-opacity="0.35"/>
  </linearGradient>
  <radialGradient id="glow" cx="50%" cy="50%">
    <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.25"/>
    <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
  </radialGradient>
</defs>
```

**Soft blurred blobs** — the cheapest way to make a slide feel designed. A large,
heavily-blurred, low-opacity organic shape behind the content. Two of these in different
hues, overlapping, is a complete modern background.

```svg
<filter id="soft"><feGaussianBlur stdDeviation="60"/></filter>
<ellipse cx="220" cy="180" rx="260" ry="200" fill="var(--accent)"
         opacity="0.18" filter="url(#soft)"/>
```

**Pattern fills** — dots or hatch lines in a `<pattern>`, used as the fill of a large
shape. Adds texture with no external asset.

```svg
<pattern id="dots" width="14" height="14" patternUnits="userSpaceOnUse">
  <circle cx="2" cy="2" r="1.4" fill="currentColor" opacity="0.28"/>
</pattern>
```

**`clipPath` / `mask`** — crop artwork into a circle, an angled band, or a device frame.
Instantly more composed than a bare rectangle.

**Layered translucency** — overlapping shapes at 0.2–0.4 opacity create new tones where
they intersect. This is how you get visual richness from two hues.

**Real data marks** — when you have numbers, draw actual bars/areas/lines to scale with a
proper baseline and labelled extremes. A real chart always beats an abstract "chart-like"
decoration.

**Iconography with mass** — draw icons as filled silhouettes at 2–3px equivalent weight,
not as thin outlines. Confine each to a consistent box (e.g. 48×48) so they align.

---

## 3. Visual patterns by slide purpose

Match the visual to what the slide is doing:

| Slide is doing | Use | Not |
|---|---|---|
| Stating one fact | One huge numeral (`clamp(4rem, 12vw, 9rem)`), unit + context small beneath, faint supporting graphic behind | A bullet |
| Comparing 2 things | Split-screen, each half a distinct tint field, a hard divider or diagonal seam | A two-column table |
| Showing a trend | Filled area chart w/ gradient to transparent, endpoints labelled, baseline visible | A line with unlabelled axes |
| Showing composition | Stacked bar or a single donut w/ one segment emphasised, rest muted | A pie with 7 similar slices |
| Showing a process | Numbered stations on a strong spine, generous spacing, current step emphasised | Boxes-and-arrows at uniform weight |
| Showing architecture | Nested tinted containers (fill-defined, not outline), grouped by shade | A hairline org chart |
| Showing time | Horizontal spine w/ weighted nodes, "now" marker, labels alternating above/below | Bulleted dates |
| Showing hierarchy | Size + tint encode level; containment beats connecting lines | A tree of thin lines |
| Section break | Full-bleed color field, huge type, minimal else | A title in the middle of white |
| Quote | Oversized type as the visual, attribution small, one accent mark | Quoted text in a bullet |
| Showing a place/thing you can't draw | A real uploaded photo if one exists — else a bold abstract composition | A crude literal drawing |

**On that last row — check for a real image first.** If the artifact has reference assets
(`artifacts_read(action='assets', artifact_id=...)`), use them: a photo or logo the user
supplied beats any composition you can draw, every time. Place it with a relative path —
`<img src="assets/hero.jpg">` — never a data URI, never an absolute URL, and never a path
you didn't see in the manifest.

Only when no such asset exists does the fallback apply: you cannot draw a photograph, and
attempting realism produces something worse than abstraction. Use a confident abstract
composition, a strong typographic treatment, or a colour field. Abstraction reads as a
deliberate design choice; a bad literal drawing reads as incompetence.

If a slide clearly wants a photo you don't have, say so in your reply — the user can drop
one onto the artifact from the Artifacts screen and you can place it on the next pass.

---

## 4. Layout, type, color, motion

**Layout.** Set a grid (12 columns, a consistent gutter, a generous margin — 6–8% of slide
width) and align everything to it. Vary the composition across slides: full-bleed, split
50/50, split 70/30, centered focal, edge-anchored. A deck where every slide is
centered-title-plus-content is monotonous even when each slide is fine. Whitespace is
structural — crowding is what makes a deck look amateur.

**Type.** One family (system stack) with a wide weight range beats two families. Set a
scale with real jumps — display / headline / body / caption at roughly 1.5–2× steps, not
1.15× (timid steps look accidental). Use `clamp()` so the deck survives projector and
laptop. Cap body text around 60 characters per line. Bold contrast between display and
body weight is most of "looks designed".

**Color.** Define everything as custom properties on `:root` and override in a
`prefers-color-scheme: dark` block. Build from: one accent, one neutral ramp (5–7 steps),
one surface, one text — plus at most one secondary accent. Check both modes: **an SVG fill
hard-coded to `#111` disappears on a dark background** — use `currentColor` or a custom
property in every fill and stroke you write.

**Motion.** One entrance transition on slide change (a short fade + 8–12px rise), 200–300ms,
consistent throughout. Optionally stagger a slide's elements by 40–60ms. Never animate
anything the audience must read, and always respect
`@media (prefers-reduced-motion: reduce)` by dropping to a plain fade.

---

## 5. Self-check before saving

Look at your deck as a whole and ask:

- Did you check `artifacts_read(action='assets')` before drawing anything representational?
- Does every `<img src>` point at a path that was actually in the manifest?
- Would this be mistaken for a wireframe or a placeholder? If yes: rules 1, 2 and 4.
- Does every visual have **fill and layered depth**, or is it lines floating in space?
- Do the graphics **fill their space**, or are they small things in big empty slides?
- Is there variety in composition across slides, or is every slide the same shape?
- **Does it work in dark mode?** Check every SVG fill and stroke, not just the CSS.
- Does the print/PDF version paginate one slide per page without clipping?
- Is any slide a bulleted list that a stronger form (number, quote, chart, split) would
  serve better?
- Is any number, quote, or name on a slide something you invented rather than sourced?
