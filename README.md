# asset-engine

Renders the image assets every app in this workspace needs from the app's own definitions:

- **Store screenshots**: localized frames for the App Store and Google Play, per device (phone, tablet) and per store page (the app's own listing and Apple custom product pages).
- **The Play feature graphic** (1024×500), built from the same pieces.
- **App Store creative assets** (iOS 27): the product page header, the search results image and the universal asset. They are built from the same pieces, on Apple's canvases, and checked against Apple's art safe area.
- **Print flyers**: laid out in millimetres with bleed, with QR codes and the forwarding pages behind them.
- **Trade-fair roll-ups**: in a print shop's exact data format, composed from an element list and checked against the rules that decide whether a banner works at a stand.
- **Profile banners** (LinkedIn 1584×396): composed from an element list and checked against where the platform covers it with the profile photo or crops it on phones.
- **App icons**: drafts drawn by an image model from a description, and every platform's icon file (iOS, Play, Android adaptive layers, splash, favicon) derived from the draft the app chose.
- **Short vertical videos** (standalone, no app): a talking-head clip with pictures and cutaways on cue, word-by-word captions and a cleaned-up voice at −14 LUFS; retakes removed and pauses shortened; alone in the frame, stacked under a screen recording, or in a bubble over it; a headline across the top.

The engine is the code. The definitions stay in the app's repo: layouts as JSON, captions per
locale, the plain app captures, the brand faces and the store pages. The app hands them over as an
`AssetProject`.

## Using it from an app

1. Install it into the app's virtualenv: `pip install -e ../asset-engine`. Once the engine is published, pin a tag in the app instead (`asset-engine @ git+https://github.com/ataudt/asset-engine.git@vX.Y.Z`).
2. Build an `AssetProject` (`asset_engine.project`) with the app's root, faces, locales and pages, plus one section per product it has: `store=StoreFiles(...)`, `flyers=PrintFiles(...)`, `rollups=PrintFiles(...)`, `banners=PrintFiles(...)`, `logos=LogoFiles(...)`. Call `set_project()` once at startup. NutriSpy does this in `marketing/asset_adapter.py` (store set and flyers), FieldFix in its own (roll-ups only). Both install it from `marketing/__init__.py`.
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
| `asset_engine.store` | slots, devices, designs and pages (`config`), composition (`compose`, `frame`), typesetting (`render`, `fonts`), captions, feature graphic, App Store creatives (`creatives`, Apple's figures in `app_store_creatives.json`), output format, CLI; the App Store Connect API (`connect`: client, asset upload, creative placement) |
| `asset_engine.printing` | what printed products share: `Sheet` (mm ↔ px, bleed, hidden strips and safety margin per edge), drawing pieces (`elements`), QR codes, forwarding pages (`links`), copy files, the PDF |
| `asset_engine.flyers` | the two-page A6 flyer, CLI |
| `asset_engine.rollups` | print-shop formats (`printers.json`), the element list (`config`, `compose`), the rules (`rules`), CLI |
| `asset_engine.banners` | platform formats and their photo and crop zones (`platforms.json`), the element list (`config`, `compose`), the zone checks (`rules`), CLI |
| `asset_engine.logos` | icon targets and their safe circles (`platforms.json`), the app's logo config (`config`), the draft prompt (`prompt`), drafts from the app's image model (`generate`), the icon set from the master (`derive`), CLI |
| `asset_engine.video` | cues, pops, voice and the ffmpeg render (`render`), removals, pauses and the cut's clock (`cuts`), the layouts, the second clip's track and the headline (`layout`), transcript cache, corrections, ASS and SRT captions (`captions`), what YouTube and TikTok accept for upload text (`platforms.json`), CLI |

## App Store creatives

Apple's creative assets appear above the screenshots: on the product page, in search results, and in
featuring. `asset_engine.store.creatives` composes them the way the feature graphic is composed:

- the set's own billboard and scrim as the ground,
- frames of the set standing on it as blocks (`feature_graphic.draw_blocks`),
- the screenshot captions, by number, as the words.

The phones show the reader's own captures, since at these sizes the interface can be read.

What Apple accepts is `store/app_store_creatives.json`, read off Apple's specification with the
date:

| Placement | Aspect | Size | Art safe area | Formats |
|---|---|---|---|---|
| `header` | 21:9 | 3840×1646 | 1646×661 at (1097, 493) | png only (the API's reference data; the help page also lists jpeg) |
| `search-results` | 3:2 | 1920×1280 to 3840×2560 | 2168×1030 at (836, 765), stated for the maximum | jpeg, png |
| `universal` | 16:9 | 5244×2950 | 1402×962 at (1921, 660) | png only |

The app lays out the placements it wants in the file `StoreFiles.creatives` names:

- `name_template` and shared styles at the top: `frame`, `shadow`, `scanner_style`, `chip_style`,
  `text`, `subline`.
- One entry per placement, keyed by its name above. It holds a `ground`, optionally its own
  `scrim` (`null` for none; leaving it out keeps the screenshots'), `blocks` (feature-graphic
  blocks: a slot plus a `capture` number) and `texts` (`{"caption": N, "subline": true, ...}` plus
  a band).
- An element is `role: "key"` by default and must lie inside the art safe area, judged on the box
  it actually drew. `"aux"` marks the atmospheric art that may run to the edges.

`<prog> creatives [--language …] [--placement …] [--out DIR] [--guides]` writes
`<set>/<lang>/<name>.jpg`, or `.png` for a placement Apple takes as PNG only. The image is flattened
to RGB, with no alpha. Findings (overflowing type, key art past the safe area) are printed loudly and
never withhold the file. `--guides` writes a reduced sheet with the safe area and every element's
box drawn in, under `_creative_guides/<lang>/` beside the sets.

### Placing them

`asset_engine.store.connect` places the rendered files on an iOS version's localizations through the
App Store Connect API, which fastlane's `deliver` does not model (install with the `store` extra).
The engine holds no key: the app builds the client from its own config and passes its bundle id and
the locales to place:

```python
from asset_engine.store.connect import AppStoreConnectClient
from asset_engine.store.connect.creatives import upload_creatives

client = AppStoreConnectClient(key_getter=get_app_store_connect_api_key)  # the app's config getter
upload_creatives(client=client, bundle_id="com.example.app", locales={"en": "en-US", "de": "de-DE"},
                 execute=False)  # a plan; execute=True places
```

Each image is filed under a reference name carrying its file's MD5, so a run sends only what
changed, and placements ride the version: they are reviewed with it. `client.py` takes
`forbidden_resources` for a caller that must never write the main listing (custom product pages);
`assets.py` also uploads screenshot sets.

## Roll-ups

An app's roll-up config names a print format from `printers.json`, a `dpi`, and the distances the
headline and the running text are read from. The layout is an ordered list of elements:
`background`, `photo`, `panel`, `image`, `text`, `list` and `qr`. Heights are millimetres above the
floor of the visible banner.

`<prog> report` composes the banner and judges what was drawn (`rollups/rules.py`, each threshold
with its source). No finding stops a build: `build` always writes everything and prints the
findings in a loud `!!` block.
- **Errors** (expected to fail at the stand):
  - content outside the visible safe area
  - text or code below 500 mm
  - headline over 7 words, or over 35 words in all
  - headline capitals under 10 mm per metre
  - running x-height under distance/250
  - contrast under WCAG AA
  - QR narrower than 80 mm
  - type that overflows its band (drawn anyway, at its smallest size)
- **Warnings:**
  - logo below 1700 mm
  - QR centre outside 600–1500 mm
  - more than 2 families
  - any picture printing under 75 dpi

`build` writes the PDF (the whole data format), `preview.png` (the visible area) and `guides.png`
(the hidden strips, the safety margin, the table zone and the eye-level band drawn in).

**No build, of any product, withholds output because of a finding** (`printing/report.py`).
A judgement (a rule finding, type that overflows) is drawn as it falls and printed loudly; what
cannot be drawn at all (a missing capture, picture or badge, a broken config) fails loudly and
removes that unit's previous output, so a stale file is never left looking current.

The PDF is RGB, which the listed shops convert themselves. CMYK with an ICC output intent and PDF/X
would need pikepdf, plus an ECI profile that may be embedded but not redistributed, so it stays out
of the repo. It is a later addition, not an assumption.

## Profile banners

A banner config names a platform from `platforms.json` and lays out an element list in that
platform's own pixels: `gradient` (a ground with soft glows), `text` (one line, shrunk to its width),
`phone` (the store bezel around a capture) and `tile` (an app icon with rounded corners). `scale`
renders the same numbers at a multiple, for sharp screens. Elements with `role: "key"` are checked
against the platform's zones: they must stay clear of the profile photo (desktop and both mobile
placements) and inside the narrowest phone crop, except across edges they `bleeds` off on purpose.
`build --guides` also writes a sheet showing the zones, the desktop profile and the phone crop with
the photo drawn over each. The zones come from design guides that disagree; the most cautious figure
is used and the others are named in the file.

## App icons

Three steps, of which only the first is not reproducible:

1. `<prog> generate "a fox curled into a circle" -n 4` asks the app's image model for drafts and writes
   them, with a `run.json` holding the prompt and each call's outcome, into a fresh
   `<default_out_dir>/drafts/<time>/`. `--from <draft.png> "thicker lines"` revises a draft instead.
2. Copy the draft you want to the `master` path of the logo config and commit it. From here on it is
   an input like any other.
3. `<prog> build` derives every target from the master and writes it to the path the config gives;
   `--out DIR` writes `<target>.png` there instead, `--guides` adds `guides.png` (each target with
   its safe circle and viewport, and under the platforms' masks).

The drafts are drawn on a flat `key_color`, which `build` cuts away. Models paint it only roughly, so
the key cut is the median of the master's border, and a border far from `key_color` is reported. Each
target places the motif by its bounding circle at `fill` (per target: `fill_by_target`) of the
platform's safe circle, from `platforms.json` with its sources.

**Credentials stay in the app.** The engine reads none: the app's `LogoFiles.google_client` is called
with the configured Vertex location and returns a ready `google.genai.Client`, built from the app's own
secrets. The client library is the `logos` extra (`pip install -e '../asset-engine[logos]'`).

```json
{
  "model": "gemini-3-pro-image",
  "location": "global",
  "key_color": "#FF00FF",
  "background": "#14213D",
  "fill": 0.86,
  "fill_by_target": {"ios-app-store": 0.72, "play-store": 0.72},
  "style": "Warm, calm and modern; two or three colours.",
  "master": "assets/logo/master.png",
  "default_out_dir": "out/logos",
  "targets": {
    "ios-app-store": "assets/icon.png",
    "android-adaptive-foreground": "assets/android-icon-foreground.png",
    "android-adaptive-monochrome": "assets/android-icon-monochrome.png",
    "splash-icon": "assets/splash-icon.png"
  }
}
```

Vertex serves the Gemini image models in `global` and `us-central1` only, not in an EU region. A logo
description holds no user data.

## Videos

Standalone: it needs no app and no `AssetProject`. Install the extra once
(`pip install -e '.[video]'`: ffmpeg from the wheel and faster-whisper), then:

```bash
~/.pyenv/versions/asset-engine/bin/python -m asset_engine.video render RAW.mp4 cues.txt \
    --out-dir edited/ --name "kalorien app" --mirror --trim auto --fix fixes.txt
```

- **Cue file**, one picture per line, times in the raw clip, picture paths relative to the file:

  ```
  00:06.9  ../raw/IMG_3421.PNG  unten-rechts  bis 00:08.6  w=40%
  12.0     foto.jpg             x=30% y=40%   1.5s         pop=aus
  ```

  Positions: `mitte`, `oben`, `unten`, `links`, `rechts`, the four corners (`oben-links` …, English
  works too), or `x=`/`y=` inside the zone the platforms leave uncovered.
- **Corrections** (`--fix`): one `wrong => right` per line, applied to the transcript before the
  captions are drawn. The transcript is made once with Whisper into `words.json` beside the cue file.
- **`--trim auto`** cuts from 0.2 s before the first sound to 0.35 s after the last, measured on
  the level. **`--mirror`** undoes a selfie camera's mirroring.
- **Cuts inside the clip.** `words` prints the transcript with times and pauses; from it the removals
  are written into a file, one per line in the raw clip's time, and passed as `--cuts`:

  ```
  26.60 - 34.10   # first take of the sentence
  ```

  `--tighten [SECONDS]` shortens every pause to that length (0.35 s), half kept at each side; a
  removal that touches silence leaves the same single pause. Cues, words and the second clip's track
  stay in the raw clip's time and are moved onto the cut by the render. `--punch 0.08` enlarges every
  second segment, so a cut reads as a change of shot (in a field, or with `--fit crop`).
- **Layouts.** `--layout stack --screen REC.mp4` puts a second clip above the speaker (`--split`, the
  upper field's share, default 0.6; `--face-anchor 50%,16%` says which part of the speaker stays),
  with the captions on the seam. `--layout pip` fills the frame with the second clip and shows the
  speaker in a bubble (`--bubble-anchor`). The second clip plays without sound. `--screen-track` ties
  stretches of it to moments of the speech:

  ```
  00:00    von 00:21 bis 00:24   fokus=10%     # speech time, then the second clip's stretch
  00:12.7  von 00:27 bis 00:30   fokus=10%
  00:15.6  von 00:30             fokus=100%    # the lower end of a phone's screen
  ```

  A stretch longer than its slot is sped up to fit, a shorter one plays as recorded and holds its
  last frame. Without a track the whole clip is fitted to the cut. `fokus` is the height of the
  recording to look at: a phone's screen is taller than the field.
- **Cutaways.** A cue with `voll` (`full`) fills the frame for its time; a video as a cue's file is
  always shown that way, without its sound.
- **Headline.** `--title "…" --title-font face.ttf` sets dark type on a white box across the top of
  the safe zone, throughout or for `--title-until` seconds.
- A caption group that would reach past the frame's edge (a long German compound) is drawn narrower.
- **`--denoise`** separates the voice from wind and other changing noise with DeepFilterNet, once,
  into `voice.wav` beside the cue file. The transcript, `--trim auto` and the render all read that
  file, so a windy clip, where the level never drops to silence, still cuts. It needs DeepFilterNet's
  standalone binary and model in the virtualenv:

  ```bash
  V=~/.pyenv/versions/asset-engine
  curl -L -o $V/bin/deep-filter \
    https://github.com/Rikorose/DeepFilterNet/releases/download/v0.5.6/deep-filter-0.5.6-x86_64-apple-darwin
  chmod +x $V/bin/deep-filter       # aarch64-apple-darwin on Apple silicon
  mkdir -p $V/share/deep-filter && curl -L -o $V/share/deep-filter/DeepFilterNet3_onnx.tar.gz \
    https://github.com/Rikorose/DeepFilterNet/raw/v0.5.6/models/DeepFilterNet3_onnx.tar.gz
  ```
- **HDR clips** (iPhone HLG) are tone-mapped to SDR BT.709 on the way in.
- **Output**, named after the camera's recording date: `2026-11-16_kalorien-app.mp4`, and beside it
  `.srt` (caption track for YouTube), `.cover.jpg` (middle of the first picture, or `--cover-at`;
  `--cover-title "…" --cover-font face.ttf` sets a headline across its top) and
  `.transcript.txt` (what is said and shown, on the cut's clock). `sidecars` writes only these three
  for an existing render.
- **Upload text**: written by hand (in a Claude session) from the transcript, into
  `<stem>.upload.md`, against the limits in `video/platforms.json`.

## Development

```bash
~/.pyenv/versions/asset-engine/bin/python -m pytest
```

Most of the coverage runs in the apps, which sweep their own definitions with the engine:
every locale, slot, capture and campaign.
