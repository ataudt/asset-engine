"""The prompt an image model draws an app-icon draft from.

Layer: asset engine.
Rules:
  - Every sentence says what the image is, never what it is not: a model fills a prohibition from its
    own prior, and the build needs exactly one thing from the draft, a motif on a flat ``key_color``
    it can cut away.
  - The motif fills about 60 % of the square, so the build can shrink it into any safe circle without
    the model having cropped it at an edge.
  - The description and the app's house style are the app's words; this module adds only the shape
    of the image the build can use.
"""

from __future__ import annotations

from asset_engine.logos.config import RGB, hex_color

MOTIF_SHARE = "about 60%"


def _canvas(key_color: RGB) -> str:
    key = hex_color(key_color)
    return (
        f"The motif is centred and fills {MOTIF_SHARE} of a square canvas. "
        ## Asked for "an app icon", the model draws the icon's rounded-square tile around the motif
        ## (kcalculate, 2026-11-19); the platform supplies that shape, so the motif stands bare. The
        ## sentence names only the silhouette: naming the tile, even to place it, drew it again.
        f"The motif stands bare on the background: its silhouette is the object's own outline, with "
        f"{key} directly around it on every side. "
        f"The background is one perfectly flat, uniform colour, exactly {key}, edge to edge, "
        "so the motif can be cut out cleanly. "
        f"The motif's colours are clearly distinct from {key}, and its outline is crisp."
    )


def draft_prompt(description: str, *, style: str, key_color: RGB) -> str:
    """A new draft from the description."""
    parts = [
        f"An app icon motif: {description.strip()}",
        "One single, bold, purely pictorial symbol in flat vector style, made of simple geometric "
        "shapes with a small palette, readable at 48 pixels.",
        style.strip(),
        _canvas(key_color),
    ]
    return "\n".join(part for part in parts if part)


def refine_prompt(change: str, *, style: str, key_color: RGB) -> str:
    """A revision of the attached draft."""
    parts = [
        f"Revise the attached app icon motif: {change.strip()}",
        "Keep everything the request leaves untouched as it is in the attached image.",
        style.strip(),
        _canvas(key_color),
    ]
    return "\n".join(part for part in parts if part)


__all__ = ["MOTIF_SHARE", "draft_prompt", "refine_prompt"]
