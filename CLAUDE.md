# asset-engine — agent notes

The shared renderer for store screenshots, the Play feature graphic, App Store creative assets,
print flyers, trade-fair roll-ups, profile banners and app icons, plus short vertical videos
(standalone, see below). It is a real dependency of the apps, not a fork: NutriSpy (store set, flyers) and FieldFix
(roll-ups) install it, `brand-creator/` (profile banners, the mark, short videos) has it editable in its own venv, and nothing is copied
back and forth. Interpreter:
`~/.pyenv/versions/asset-engine/bin/python`. Both apps' venvs have it installed editable, so an edit
here is live there. Public on GitHub as `ataudt/asset-engine`; an app's `setup.py` pins a tag.

# Code here, definitions in the app

- Nothing app-specific belongs in this repo: no locale list, no brand face, no product name, no
  path into an app's tree. An app supplies these through `AssetProject`. A value only one app has is
  a field there, or JSON in that app.
- No path is derived from where the engine's own files sit. Every relative path resolves against
  `get_project().root`.
- The engine never imports an app. What it needs to ask (store pages, chip words) is a method on
  `PageRegistry` or a callable on `AssetProject`.
- The engine holds no credentials and reads no `.env`. A model client is built by the app from its
  own secrets and handed over as a callable (`LogoFiles.google_client`); so are the store keys
  (`PublishFiles.apple_key` / `play_key`, `AppStoreConnectClient(key_getter=…)`, `PlayClient`).

# Publishing reads before it writes

`asset_engine.store.publish` (an app's `python -m marketing.publish`) puts an app's listing and
subscriptions on both stores from its listing source (`store.listing`: `appstore/locales/…`,
`locales.config.json`, `update/{apple,google}.md`, `store.json`, `subscriptions.json`). It replaces
the fastlane lanes and the listing builders the apps used to fork.

- Every step reads the store first and plans. It writes only with `--apply`, and only what differs;
  images are compared by checksum, so an identical image is never sent back into review. A plan
  that finds nothing (`Report.changes == 0`, `--check`) is how a migration proves the repo describes
  the store as it stands.
- What a person decides in a console is reported, never forced: a billing period, a region taken
  away from subscribers, a price raise for existing subscribers, a language removed.
- Copy is taken as written. A value may differ per store (`{"apple": …, "android": …}`) where a
  feature works differently on each platform; the engine never rewrites text.

# Videos stand alone

`asset_engine.video` is for social-media content in general, not for an app: it runs as
`python -m asset_engine.video` from this venv, has no `AssetProject`, and takes every input (clip,
cue file, pictures, corrections, removals, a second clip and its track) on the command line.
`brand-creator` is its consumer: a short's folder there becomes these arguments
(`brand_creator/shorts/contract.py`), so a renamed flag breaks it. The upload text (titles,
description, hashtags) is written by Claude in the session from the `<stem>.transcript.txt` the
render leaves beside the clip, against the limits in `video/platforms.json`; no code calls a model.
The same holds for which of two takes is removed: Claude writes the removals from the transcript
with times (`words`), the code only applies them. Outputs are named `<recording date>_<1-3 words>`.

Every time a person writes (cues, removals, the second clip's track) is the raw speech clip's; only
the render moves it onto the cut (`cuts.Timeline`). One uncut clip alone in the frame goes through
the graph it always did, so its render does not change when cuts and layouts gain features.

# Output is a function of its inputs

Every render is byte-for-byte a function of the app's files and of this engine's version. Committed
outputs in the apps (website screens, share cards) depend on it.

- A change here that alters pixels is a change to every consuming app's committed output. Say so in
  the commit message, bump the version, and the app rebuilds what it commits when it moves its tag.
- A change that must not alter pixels is checked by rendering before and after into a scratch
  directory and comparing hashes. Do not check it by rebuilding an app's committed tree.
- Pillow and segno are pinned exactly in `pyproject.toml`. Raise a pin only as a deliberate,
  pixel-changing release.
- `logos generate` is the one step that is not a function of its inputs. It writes only into a fresh
  drafts directory; a draft becomes an input when the app commits it as its `master`, and `build`
  derives the icon set from that with Pillow alone.

# Print formats and roll-up rules are facts with sources

- `rollups/printers.json` holds print-shop data formats exactly as the shop's data sheet states
  them, with the URL and the date read. A new shop is a new entry read off its data sheet. Never
  adjust one shop's numbers to fit another: the formats are not interchangeable.
- `logos/platforms.json` holds each icon target's size, layer and the circle no platform mask clips,
  read off Apple's, Android's, Play's, Expo's and Google Search's guides, with the date read.
- `store/app_store_creatives.json` holds each App Store creative placement's accepted sizes, aspect,
  art safe area and formats exactly as Apple's specification states them, with the URL and the date
  read.
- `store/app_store_iphone.json` holds what the App Store on an iPhone shows of them, measured on
  screenshots with the date: the header's window and the overlays on it, a gallery frame's width in
  points, and Apple's 11 pt type minimum. **The iPhone is the measure** (no app targets the Mac):
  key art lies in the iPhone window where a placement has one, in the safe area where it has none,
  and type arrives at 11 pt or more. A layout that fails is fixed in the app's `creatives.json` or
  `config.json`; a measurement changes only on a new screenshot.
- `banners/platforms.json` holds a platform's banner canvas and the zones its profile photo covers
  and its phone view crops, with the guides' URLs and the date read. LinkedIn publishes no figures,
  so the guides disagree; the file keeps the most cautious and names the rest.
- `rollups/rules.py` thresholds each name their source. A threshold changes because a better source
  says so, not because a layout fails it. A failing layout is fixed in the app's config.
- **No build withholds output.** Findings and overflowing type are drawn and printed loudly;
  only what cannot be drawn at all fails, and it removes that unit's old files. A withheld output
  leaves the previous file standing, which is then judged as if it were the current code.

# Before changing an API

`brand-creator/` runs `asset_engine.banners` and `.logos` through `python -m marketing.banners` / `marketing.logos`, and
`asset_engine.video` through `python -m brand_creator short`.
FieldFix (roll-ups) runs `asset_engine.rollups` through `python -m marketing.rollups`; after a
change there, run `~/.pyenv/versions/fieldfix/bin/python -m pytest marketing/rollups` from
`FieldFix-App/`. NutriSpy imports these modules directly (`asset_engine.store.config`, `.compose`, `.captions`,
`.output`, …) from its website builders and tests. All three store apps (NutriSpy, kcalculate, Wunschkalender) publish through
`asset_engine.store.publish` (`python -m marketing.publish`), and their listing sweeps import
`asset_engine.store.listing`. After a change in `store.listing`, `store.connect` or `store.play`, run
the three suites and, in each app, the plans with `--check` (`listing`, `record`, `subscriptions`
for both stores): a plan that is not empty where it was empty before is the regression. Renaming or reshaping a public
name breaks them. Run NutriSpy's suite (`~/.pyenv/versions/nutrispy/bin/python -m pytest` from
`NutriSpy-App/`) after any such change. Its sweeps are where most of this engine's coverage
lives.
