# asset-engine

Renders the image assets every app in this workspace needs from the app's own definitions:

- **Store screenshots**: localized frames for the App Store and Google Play, per device (phone, tablet) and per store page (the app's own listing and Apple custom product pages).
- **The Play feature graphic** (1024×500), built from the same pieces.
- **Print flyers**: laid out in millimetres with bleed, with QR codes and the forwarding pages behind them.
- **Trade-fair roll-ups**: in a print shop's exact data format, composed from an element list and checked against the rules that decide whether a banner works at a stand.

The engine is the code. The definitions stay in the app's repo: layouts as JSON, captions per
locale, the plain app captures, the brand faces and the store pages. The app hands them over as an
`AssetProject`.

## Using it from an app

1. Install it into the app's virtualenv: `pip install -e ../asset-engine`. Once the engine is published, pin a tag in the app instead (`asset-engine @ git+https://github.com/ataudt/asset-engine.git@vX.Y.Z`).
2. Build an `AssetProject` (`asset_engine.project`) with the app's root, faces, locales and pages, plus one section per product it has: `store=StoreFiles(...)`, `flyers=PrintFiles(...)`, `rollups=PrintFiles(...)`. Call `set_project()` once at startup. NutriSpy does this in `marketing/asset_adapter.py` (store set and flyers), FieldFix in its own (roll-ups only). Both install it from `marketing/__init__.py`.
3. Give each CLI an entry point in the app. The entry passes its own `prog` and may add commands of its own:

   ```python
   # <app>/marketing/appstore_screenshots/__main__.py
   import sys
   from asset_engine.store.cli import main

   sys.exit(main(prog="python -m marketing.appstore_screenshots"))
   ```

`PageRegistry` describes an app with one store page. An app with custom product pages subclasses
it.

## Layout

| Package | What |
|---|---|
| `asset_engine.project` | `AssetProject` and its product sections, `PageRegistry`, `set_project` / `get_project` |
| `asset_engine.store` | slots, devices, designs and pages (`config`), composition (`compose`, `frame`), typesetting (`render`, `fonts`), captions, feature graphic, output format, CLI |
| `asset_engine.printing` | what printed products share: `Sheet` (mm ↔ px, bleed, hidden strips and safety margin per edge), drawing pieces (`elements`), QR codes, forwarding pages (`links`), copy files, the PDF |
| `asset_engine.flyers` | the two-page A6 flyer, CLI |
| `asset_engine.rollups` | print-shop formats (`printers.json`), the element list (`config`, `compose`), the rules (`rules`), CLI |

## Roll-ups

An app's roll-up config names a print format from `printers.json`, a `dpi`, and the distances the
headline and the running text are read from. The layout is an ordered list of elements:
`background`, `photo`, `panel`, `image`, `text`, `list` and `qr`. Heights are millimetres above the
floor of the visible banner.

`<prog> report` composes the banner and judges what was drawn (`rollups/rules.py`, each threshold
with its source):
- **Errors stop the build:**
  - content outside the visible safe area
  - text or code below 500 mm
  - headline over 7 words, or over 35 words in all
  - headline capitals under 10 mm per metre
  - running x-height under distance/250
  - contrast under WCAG AA
  - QR narrower than 80 mm
- **Warnings:**
  - logo below 1700 mm
  - QR centre outside 600–1500 mm
  - more than 2 families
  - any picture printing under 75 dpi

`build` writes the PDF (the whole data format), `preview.png` (the visible area) and `guides.png`
(the hidden strips, the safety margin, the table zone and the eye-level band drawn in).

The PDF is RGB, which the listed shops convert themselves. CMYK with an ICC output intent and PDF/X
would need pikepdf, plus an ECI profile that may be embedded but not redistributed, so it stays out
of the repo. It is a later addition, not an assumption.

## Development

```bash
~/.pyenv/versions/asset-engine/bin/python -m pytest
```

Most of the coverage runs in the apps, which sweep their own definitions with the engine:
every locale, slot, capture and campaign.
