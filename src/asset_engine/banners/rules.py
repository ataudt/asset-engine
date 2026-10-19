"""Where a banner's key elements land against what the platform covers and crops.

Layer: asset engine.
Rules:
  - Each check judges ``compose.Drawn`` records — the boxes actually drawn — against the zones of
    ``platforms.json``, which name their sources. Only elements with ``role: "key"`` are judged; a
    ground or a decoration may run anywhere.
  - A ``keep-out`` zone is covered (the profile photo): a key element must not touch it. A ``keep-in``
    zone is what survives the narrowest crop: a key element must lie inside it, except across the
    edges it ``bleeds`` off on purpose.
  - No finding stops a build (``printing.report``). The zones are the most cautious of disagreeing
    guides, not LinkedIn's own figures: the guides preview and the real profile are what decide.
"""

from __future__ import annotations

from asset_engine.banners.compose import Drawn
from asset_engine.banners.config import Banner, Box


def _overlap(a: Box, b: Box) -> tuple[int, int]:
    return (min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1]))


def _outside(box: Box, zone: Box, bleeds: tuple[str, ...]) -> list[str]:
    """The edges of ``box`` that reach past ``zone``, with how far, ignoring the ones it bleeds off."""
    reach = {
        "left": zone[0] - box[0],
        "top": zone[1] - box[1],
        "right": box[2] - zone[2],
        "bottom": box[3] - zone[3],
    }
    return [f"{edge} by {px}px" for edge, px in reach.items() if px > 0 and edge not in bleeds]


def check(banner: Banner, drawn: list[Drawn]) -> list[str]:
    """Every finding for one composed banner, as printable lines."""
    findings = [f"{item.id}: {item.overflow}" for item in drawn if item.overflow]
    for item in drawn:
        if item.role != "key" or item.box is None:
            continue
        for zone in banner.platform.zones:
            if zone.kind == "keep-out":
                width, height = _overlap(item.box, zone.box)
                if width > 0 and height > 0:
                    findings.append(f"{item.id}: lies {width}x{height}px under {zone.name} {list(zone.box)}")
            else:
                past = _outside(item.box, zone.box, item.bleeds)
                if past:
                    findings.append(f"{item.id}: reaches past {zone.name} {list(zone.box)} — {', '.join(past)}")
    return findings


__all__ = ["check"]
