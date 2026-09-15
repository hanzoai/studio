---
version: alpha
name: Hanzo tutorial — Frame (video / frame layer)
description: >
  The Hanzo brand at frame scale. Monochrome by rule: near-black canvas, white ink, a
  neutral grey ladder. One accent, the money green from the Hanzo deck, spent only on
  prices and the words that carry the outcome. Zen for every word, Zen Mono for
  prompts, messages and checklists. The product on screen is a capture of the
  product; type and rules sit beside it, never over it.
unit: the frame — 1920×1080
principle: the screen is the star · words arrive before things · one accent, spent on money
colors:
  canvas: "#0B0B0D"
  panel: "#141416"
  panel-edge: "rgba(246,246,243,0.14)"
  ink: "#F6F6F3"
  mid: "#A1A1AA"
  dim: "#6E6E73"
  accent: "#37D77C"
  accent-2: "#B195EC"
typography:
  display: { fontFamily: "Zen", weight: 500, cqw: 5.2, lineHeight: 1.02, tracking: "-0.03em" }
  headline: { fontFamily: "Zen", weight: 500, cqw: 3.6, lineHeight: 1.06, tracking: "-0.02em" }
  lede: { fontFamily: "Zen", weight: 400, cqw: 1.6, lineHeight: 1.45, color: "mid" }
  body: { fontFamily: "Zen", weight: 400, cqw: 1.35, lineHeight: 1.5 }
  label: { fontFamily: "Zen Mono", px: 20, weight: 500, tracking: "0.14em", upper: true, color: "dim" }
  mono: { fontFamily: "Zen Mono", cqw: 1.25, weight: 400, lineHeight: 1.55 }
  number-hero: { fontFamily: "Zen", weight: 500, cqw: 9, lineHeight: 0.95, tracking: "-0.03em" }
fonts:
  Zen: assets/fonts/zen.woff2
  "Zen Mono": assets/fonts/zen-mono.woff2
spacing:
  edge: "80px"
  gap: "32px"
  radius: "14px"
components:
  window:
    description: "A capture in a window: 1px panel-edge border, 14px radius, three grey rings and a mono address in the bar, panel fill behind. The image is the content; nothing is drawn over it except a cursor or typed text in the input it already has."
  card:
    description: "Offer and upsell cards: panel fill, panel-edge border, the price in number-hero green, the lines in body ink, no shadow."
  checklist:
    description: "Zen Mono rows that check off one at a time; a checked row's box fills accent."
---

## Overview

A faceless screen tutorial. Every frame is either a Hanzo screen in a window or a plain typographic beat on the canvas. The palette is the site's: no hue except the accent, and the accent is money.

## The Frame

Left column for words when a screen is present (copy at 40% width, screen at 60%). Full-bleed type when there is no screen. 80px edge. Captions live in the bottom band, above nothing that matters.

## Composition Rules

- Do: keep the capture unmodified; zoom with the camera, never repaint it.
- Do: one accent per frame at most — a price, a checked box, a highlighted phrase.
- Don't: gradients, glows, drop shadows, a second hue.
- Don't: invent product screens. If a screen does not exist as a capture, show type.
