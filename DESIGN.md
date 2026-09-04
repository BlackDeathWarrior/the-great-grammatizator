# Design

## Register

Product. The design serves the work; it is not the product.

## The scene

An operator at a desk, mid-afternoon, seven artefacts generating at once
against a deadline. Not a dark room, not 2am - this is ordinary working light
on an ordinary working day, and the screen sits alongside a mail client and a
document. That argues against the reflexive "dark tooling" default: a light
ground reads as a working document, which is what this is.

Dark stays available and correct via prefers-color-scheme, because plenty of
operators run their whole desktop dark.

## Colour

OKLCH throughout. Never #000 or #fff: every neutral is tinted toward the ink
hue so the greys agree with each other.

Strategy: restrained. One accent, used for action and focus only. The semantic
trio - pass, flag, fail - is the only other colour, and it is reserved for
verdicts. Nothing decorative is ever green.

## Type

System stack, no webfont: a demo must not wait on someone else's CDN. Scale
steps at 1.25 minimum so hierarchy survives a glance. Numbers - scores, counts,
retries - are tabular so a column of them lines up.

## Motion

Diegetic only. Something moves because work happened: a checker resolved, a
count advanced, a score changed. Ease-out curves, 120-260ms. Nothing bounces.

Every animation sits behind prefers-reduced-motion.

## Rules specific to this interface

- The status region re-renders on a poll. Anything the operator can open, type
  into, or scroll must survive that, so per-artefact state is keyed and swapped
  out of band rather than wholesale.
- Verdict is never carried by colour alone: every chip states pass or fail in
  words as well.
- The interview is the dominant path; the lists are the escape hatch for
  someone who already knows exactly what they want.
