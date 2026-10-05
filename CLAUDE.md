# asset-engine — agent notes

The shared renderer for store screenshots, the Play feature graphic and print flyers. It is a real
dependency of the apps, not a fork: NutriSpy (and later FieldFix) installs it, and nothing is
copied back and forth. Interpreter: `~/.pyenv/versions/asset-engine/bin/python`. NutriSpy's venv
has it installed editable, so an edit here is live there.

# Code here, definitions in the app

- Nothing app-specific belongs in this repo: no locale list, no brand face, no product name, no
  path into an app's tree. An app supplies these through `AssetProject`. A value only one app has is
  a field there, or JSON in that app.
- No path is derived from where the engine's own files sit. Every relative path resolves against
  `get_project().root`.
- The engine never imports an app. What it needs to ask (store pages, chip words) is a method on
  `PageRegistry` or a callable on `AssetProject`.

# Output is a function of its inputs

Every render is byte-for-byte a function of the app's files and of this engine's version. Committed
outputs in the apps (website screens, share cards) depend on it.

- A change here that alters pixels is a change to every consuming app's committed output. Say so in
  the commit message, bump the version, and the app rebuilds what it commits when it moves its tag.
- A change that must not alter pixels is checked by rendering before and after into a scratch
  directory and comparing hashes. Do not check it by rebuilding an app's committed tree.
- Pillow and segno are pinned exactly in `pyproject.toml`. Raise a pin only as a deliberate,
  pixel-changing release.

# Print formats and roll-up rules are facts with sources

- `rollups/printers.json` holds print-shop data formats exactly as the shop's data sheet states
  them, with the URL and the date read. A new shop is a new entry read off its data sheet. Never
  adjust one shop's numbers to fit another: the formats are not interchangeable.
- `rollups/rules.py` thresholds each name their source. A threshold changes because a better source
  says so, not because a layout fails it. A failing layout is fixed in the app's config. Warnings
  (low picture resolution, logo or QR outside its band) do not stop a build.

# Before changing an API

FieldFix (roll-ups) runs `asset_engine.rollups` through `python -m marketing.rollups`; after a
change there, run `~/.pyenv/versions/fieldfix/bin/python -m pytest marketing/rollups` from
`FieldFix-App/`. NutriSpy imports these modules directly (`asset_engine.store.config`, `.compose`, `.captions`,
`.output`, …) from its metadata builders, website builders and tests. Renaming or reshaping a public
name breaks them. Run NutriSpy's suite (`~/.pyenv/versions/nutrispy/bin/python -m pytest` from
`NutriSpy-App/`) after any such change. Its sweeps are where most of this engine's coverage
lives.
