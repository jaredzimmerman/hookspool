# Banners

The graphics at the top of the sections in the README and the documentation folders. Each is drawn in the
[Hairline](https://hairline.lucasmarkes.com) style: isometric line figures made of thin strokes, one bright mark per figure,
and a light and a dark version of every one.

There is one figure per section, and each stands for what the section says:

| figure | file | where it appears | what it stands for |
|---|---|---|---|
| brake | `brake-{light,dark}.gif` | README, top | a lever holds back the outgoing thread while the spool fills, then lets it pay out |
| letterbox | `letterbox-{light,dark}.svg` | README, The problem | letters drop through a slot, and the ones that miss the basket are lost |
| ratchet | `ratchet-{light,dark}.svg` | README, What it guarantees | a wheel that only turns forward, and what is held stays held |
| beads | `beads-{light,dark}.gif` | README, How it works | beads that arrive close together drop into the cup as one batch |
| spindle | `spindle-{light,dark}.svg` | README, Quick start | a bobbin set onto an empty spindle |
| rack | `rack-{light,dark}.svg` | README, Bobbins | one bobbin per outside service, all built the same way |
| dials | `dials-{light,dark}.svg` | Configuration | three knobs, each set to its own value |
| breakers | `breakers-{light,dark}.svg` | Operations | a breaker that trips, cools down and recovers |

The SVG files are static exports of the figure at rest. The two GIFs are recordings of the figure's play lap, because GIF
displays everywhere a README is read. A Markdown file embeds one like this, so the right version shows in each color scheme:

```html
<picture><source media="(prefers-color-scheme: dark)" srcset="docs/assets/banners/rack-dark.svg"><img alt="Describe the drawing in one plain sentence." src="docs/assets/banners/rack-light.svg" width="960"></picture>
```

`tests/test_docs.py` keeps the set honest: no SVG here has a script, an event attribute or a reference to anything outside
the file, every banner a Markdown page names exists, and each GIF is under 1 MB.
