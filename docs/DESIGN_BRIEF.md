# Design Brief — the run timeline and the HTML report

**Date:** 2026-08-03 · **PRD:** [PRD.md](PRD.md) · **App Flow:** [APP_FLOW.md](APP_FLOW.md)

Two visual surfaces exist, both generated, both dependency-free, both pure functions of a
run: the SVG timeline (`raftverified/timeline.py`, ~200 lines of hand-written SVG) and the
standalone HTML report (`raftverified/report.py`, inline CSS, inline SVG, no external
asset of any kind). Nothing else has a visual surface — the CLI is deliberately plain text.
This brief covers those two, with measured numbers rather than intentions.

## Intent

A **lab instrument, not a dashboard.** Someone should be able to look at 20,000 steps of
chaos and see, in one glance, when each node led, when it was down, when the cluster was
split, and where commits happened — then point at a pixel and say "that election, there".

What it must never feel like: a status page. No gauges, no traffic lights, no smoothing, no
"health score". If a run was ugly, the picture is ugly, because the ugly part is the
evidence. The one and only judgement the report renders is the linearizability verdict,
and it is a factual PASS/FAIL badge with the oracle's own message next to it.

## Who is looking at it

Two people, both with about a minute:

1. **The author, mid-debug**, who already has a failing seed and needs to see *where* in
   virtual time the interesting thing happened before opening the trace.
2. **A reviewer with no context**, who opens `docs/showcase-report.html` from a link. This
   person will not read the axis legend first. The picture has to be legible before it is
   understood, and the legend has to be there when they look for it.

Neither is on a phone; both are at a desk with a browser or an SVG viewer.

## Precedents

- **Jepsen's Knossos/analysis plots** — the specific thing they get right is that *time is
  the x-axis and a process is a lane*, so concurrency is spatial rather than described.
- **Chrome DevTools' performance flame timeline** — the specific thing: a background band
  for a global condition (here, an active partition) rather than an annotation per lane, so
  "everything was broken during this window" reads without repetition.
- **`git log --graph`** — the specific thing: legibility in a monospace grid with no
  chrome, and no colour required to understand the structure.

## Anti-patterns for this project

- **Animation of any kind.** The run is already over; motion would imply a live system and
  would break byte-identical output.
- **Interactive tooltips or JavaScript.** The report must open from a file:// URL in ten
  years. It contains zero script tags.
- **Any external request** — a font CDN, an icon set, a chart library. The report is
  tested for it: `test_report_is_self_contained_html` asserts no `http://` appears other
  than the SVG XML namespace.
- **Rounding away the ugly parts.** No axis smoothing, no clamped outliers, no "≈".
- **A colour scale that implies magnitude.** Term numbers are nominal, not ordinal; a
  sequential ramp would suggest term 9 is "more" than term 2.

## Type

One family each, no exceptions. The SVG is entirely `font-family="monospace"` — it is a
technical diagram and column alignment matters more than personality. The HTML report uses
the system UI stack (`-apple-system, Segoe UI, Roboto, sans-serif`) at 14px/1.5 for prose,
with `font-variant-numeric: tabular-nums` on every table cell so digits line up down the
column, and a monospace `<code>` for hashes and commands. Sizes in use: 20px h1, 14px
uppercase-tracked h2, 14px body, 13px code, 12px card labels; in the SVG, 14px title, 11px
node labels, 10px legend, 9px axis.

## Colour

| Role | Value | Where |
|---|---|---|
| surface | `#fafafa` page, `#fff` cards/table/timeline panel | report |
| text | `#1a1a1a` | report body |
| muted | `#555` (h2), `#777` (subtitle), `#888` (card labels), `#aaa` (footer) | report |
| success | `#1e7e34` on `#e6f4ea` | LINEARIZABLE pill |
| danger | `#c62828` on `#fdecea` | NOT LINEARIZABLE pill |
| term identity | 8-colour categorical palette (Tableau-10 derived), cycling | SVG bars |
| crashed | `#c8c8c8` | SVG bars |
| election / partition | `#e15759` | SVG triangle, band |
| commit | `#59a14c` | SVG tick |
| snapshot | `#b07aa1` | SVG diamond |

**Encoding rules — colour is never the only signal in the SVG.** Role is encoded by bar
*thickness* (follower 8px, candidate 14px, leader 20px, leader also outlined `#333`), so
the most important distinction survives greyscale printing entirely. Events are encoded by
*shape*: triangle = election, dot = vote, tick = commit, diamond = snapshot. A leader's
term is additionally written as a text label (`L177`) at the moment it takes office. The
one genuinely colour-only encoding is *which* term a bar belongs to, and the palette cycles
every 8 terms — both facts are stated in the rendered legend rather than hidden.

## Spacing and layout

SVG: fixed 1100 × (56 + 48·nodes + 96) px, one 48px lane per node, gridline every
`duration/10` ms with a labelled tick. HTML: 24px page padding, 10px flex gap, cards
`12px 16px` with an 8px radius and a 1px `#e5e5e5` border, sections separated by 24px. The
timeline sits in an `overflow: auto` panel, so a wide SVG scrolls inside its container
instead of stretching the page.

## States

The report has no interactive states — no links, no buttons, no inputs, no hover, no
focus, nothing to disable. That is a deliberate consequence of "one static artefact",
and it is why the accessibility floor's keyboard and touch-target clauses do not apply
here rather than being quietly skipped. The content states that do exist are: verdict PASS,
verdict FAIL, and a nodes table that is never empty (a run always has at least one node).

## Accessibility floor — measured, and where it fails

Contrast computed with the WCAG relative-luminance formula against the actual background
each element sits on:

| Pair | Ratio | Verdict |
|---|---|---|
| `#1a1a1a` body on `#fafafa` | 16.67:1 | pass |
| `#555` h2 on `#fafafa` | 7.14:1 | pass |
| `#111` on white (SVG title, node labels) | 18.88:1 | pass |
| `#333` on white (SVG legend, 10px) | 12.63:1 | pass |
| `#c62828` on `#fdecea` (FAIL pill) | 4.92:1 | pass |
| `#1e7e34` on `#e6f4ea` (PASS pill) | 4.52:1 | pass, barely |
| `#777` on `#ffffff` (SVG axis, 9px) | 4.48:1 | **fail** (4.5 needed) |
| `#777` on `#fafafa` (report subtitle, 14px) | 4.29:1 | **fail** |
| `#888` on `#ffffff` (card labels, 12px) | 3.54:1 | **fail** |
| `#e15759` on white (partition label, 9px) | 3.68:1 | **fail** |
| `#aaa` on `#fafafa` (footer, 12px) | 2.23:1 | **fail** badly |

Five failures, all in secondary text, none of them in a number a reader needs. They are
recorded here rather than fixed in this pass for a specific reason: the HTML report is
pinned by a byte-exact golden digest, so a CSS change is a deliberate rebaseline and
belongs in its own commit with its own justification — not smuggled in alongside a
rename. The remedies are known and cheap: `#888 → #6b6b6b`, `#aaa → #767676`,
`#777 → #6e6e6e`, and the 9px SVG labels either darkened or enlarged.

Other floor items, honestly:

- **200% zoom / 320px width:** the report's card grid wraps (flexbox) and the timeline
  panel scrolls, so it degrades acceptably — but there is **no `<meta name="viewport">`**
  in the report's head, so a phone renders it zoomed out. A real gap, one line to fix.
- **`prefers-reduced-motion`:** trivially satisfied — there is no motion, no transition and
  no animation anywhere.
- **Colour-blindness:** the categorical palette is Tableau-derived and reasonably separable,
  and every semantic distinction (role, event type, crashed vs live) is carried by shape or
  thickness as well. Term-to-term distinction is the one that would degrade.
- **Screen readers:** the SVG has no `<title>`/`<desc>` element and no ARIA roles, so it is
  announced as an unlabelled graphic. The README supplies alt text where the image is
  embedded, which covers the common path but not the report page itself.
- **Input escaping:** the report escapes untrusted-shaped values (`html.escape` on the
  verdict message, the fault profile and the replay command); `render_timeline` inserts its
  `title` argument into the SVG unescaped. Every caller today builds that string from
  validated ints and a fixed choice list, so nothing malformed can reach it — but a library
  caller passing arbitrary text would produce broken SVG. Worth a `saxutils.escape` when
  the file is next touched.

## Done means

- [x] Reads as an instrument, not a dashboard
- [x] Every content state rendered (PASS, FAIL, crashed nodes, partition windows, snapshots)
- [x] Zero external assets — asserted by a test, not by inspection
- [x] Byte-identical output for a fixed run — asserted by a golden test
- [x] Colour never the only signal for role or event type
- [ ] Contrast: five secondary-text pairs fail, listed above with their fixes
- [ ] Viewport meta tag on the report
- [ ] SVG `<title>`/`<desc>` for screen readers
