"""How a rendered frame reaches disk: its file format, and how it is copied into a store tree.

Layer: asset engine (offline, CLI-driven).
Imports: Pillow, stdlib.

Everything downstream of the renderer reads ``FRAME_SUFFIX`` rather than spelling an extension,
because the discovery globs, the destination names and the Fastfile all have to agree: a glob that
names an extension the writer no longer produces finds nothing, and a release aborts in
``delete_rendered_screenshot_sets!`` instead of uploading.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from PIL import Image

## Both stores take JPEG: Apple's screenshot specification lists .jpg/.jpeg/.png, and Play
## documents "JPEG or 24-bit PNG (no alpha)". fastlane needs nothing either — supply globs
## {png,jpg,jpeg} and deliver's AppScreenshot whitelists the same three. At q92/4:4:4 a frame is
## ~4x smaller than the PNG with no visible difference in the caption text, which is what makes the
## ~8 GB of gitignored build output affordable to keep and quick to upload.
FRAME_SUFFIX = ".jpg"

## The extensions a frame may have carried before, so a stale sibling can be cleared. Without this,
## a tree rebuilt after the format changed holds both ``Phone 1.png`` and ``Phone 1.jpg``, and
## whichever glob matched both would hand the store two copies of every frame.
_STALE_SUFFIXES = (".png", ".jpeg", ".jpg")

JPEG_QUALITY = 92


def save_frame(image: Image.Image, dest: Path) -> None:
    """Write one rendered frame to ``dest``, replacing a same-stem file left by another format."""
    for suffix in _STALE_SUFFIXES:
        if suffix == FRAME_SUFFIX:
            continue
        dest.with_suffix(suffix).unlink(missing_ok=True)

    ## subsampling=0 is 4:4:4, and it is the argument carrying the quality here: the captions are
    ## large light text on a near-black field, which is exactly where JPEG's default 4:2:0 chroma
    ## reduction shows as coloured fringing on the glyph edges. Dropping to the default would save
    ## about a third again and visibly soften every headline in all 16 locales.
    ## progressive stays off: it saves ~3%, and neither store's asset pipeline documents what it
    ## does with a progressive scan. optimize is a cheap Huffman pass here, unlike PNG's, where it
    ## cost ~3s per frame for 5%.
    image.save(
        dest,
        format="JPEG",
        quality=JPEG_QUALITY,
        subsampling=0,
        optimize=True,
        progressive=False,
    )


def link_or_copy(src: Path, dest: Path) -> None:
    """Hardlink ``src`` to ``dest``, copying only where the filesystem will not link.

    The store trees are overwhelmingly duplicates — each custom product page overrides a frame or
    two and inherits the rest of the locale set, so the same bytes were written once per variant.
    Linking is sound *only* because nothing edits a built tree in place: both builders ``rmtree``
    the whole tree and write it again, and an unlink through one name leaves every other name
    whole. A builder that ever opened a frame for writing would have to copy instead.
    """
    dest.unlink(missing_ok=True)
    try:
        os.link(src, dest)
    except OSError:
        ## Different filesystem, or one without hardlinks. The tree is still correct, just large.
        shutil.copyfile(src, dest)
