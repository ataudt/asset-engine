"""Logo configuration — the icon targets a platform asks for, and an app's choices for its own icon.

Layer: asset engine.
Rules:
  - A target's size, safe circle and viewport are the platform's, named by key from
    ``platforms.json``; nothing here spells a size.
  - The app's logo config names the image model and its Vertex location (both required: which model
    draws an app's drafts is the app's decision), the flat ``key_color`` the drafts are drawn on and
    the build cuts away, the ``background`` behind the motif, how much of each safe circle the motif
    ``fill``s (``fill_by_target`` overrides it per target: a safe circle that is the whole square, as
    on iOS, makes the same fill look larger than inside Android's 66 dp), the ``master`` it chose, and
    where each target is written.
  - Paths resolve against ``get_project().root``; an absolute or ``~/`` path (a library outside the
    repo) is taken as is. Colours are RGB lists or ``#rrggbb``.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
from typing import Any

from asset_engine.project import get_project

PLATFORMS_PATH = Path(__file__).resolve().parent / "platforms.json"

RGB = tuple[int, int, int]
LAYERS = ("full", "foreground", "background", "monochrome")
MASKS = ("circle", "superellipse", "rounded-30")


class LogoConfigError(RuntimeError):
    """The logo cannot be generated or built as configured."""


@dataclass(frozen=True)
class Target:
    """One icon file a platform asks for (see ``platforms.json`` for what each field means)."""

    name: str
    size: int
    layer: str
    safe_circle: float
    viewport: float
    masks: tuple[str, ...]


@lru_cache(maxsize=1)
def targets() -> dict[str, Target]:
    """Every icon target the engine knows, by key."""
    raw = json.loads(PLATFORMS_PATH.read_text(encoding="utf-8"))
    known = {}
    for name, entry in raw.items():
        if name.startswith("_"):
            continue
        target = Target(
            name=name,
            size=entry["size"],
            layer=entry["layer"],
            safe_circle=entry["safe_circle"],
            viewport=entry["viewport"],
            masks=tuple(entry["masks"]),
        )
        if target.layer not in LAYERS or not set(target.masks) <= set(MASKS):
            raise LogoConfigError(f"platforms.json: target {name!r} has layer {target.layer!r} / masks {target.masks}; known: {LAYERS} / {MASKS}")
        known[name] = target
    return known


def target(name: str) -> Target:
    known = targets()
    if name not in known:
        raise LogoConfigError(f"no icon target {name!r} — platforms.json has {sorted(known)}")
    return known[name]


def color(value: str | list[int], *, field: str) -> RGB:
    """An RGB list or ``#rrggbb``."""
    if isinstance(value, str):
        hex_digits = value.removeprefix("#")
        if len(hex_digits) != 6:
            raise LogoConfigError(f"{field}: {value!r} is not #rrggbb")
        return (int(hex_digits[0:2], 16), int(hex_digits[2:4], 16), int(hex_digits[4:6], 16))
    if len(value) != 3 or not all(isinstance(channel, int) and 0 <= channel <= 255 for channel in value):
        raise LogoConfigError(f"{field}: {value!r} is not an RGB list")
    return tuple(value)  # type: ignore[return-value]


def hex_color(rgb: RGB) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


@dataclass(frozen=True)
class LogoConfig:
    model: str
    location: str
    key_color: RGB
    background: RGB
    ## The motif's bounding circle as a fraction of each target's safe circle; ``fill_for`` applies
    ## the per-target overrides.
    fill: float
    fill_by_target: dict[str, float]
    ## A sentence or two of house style the prompt carries for every draft ("" for none).
    style: str
    master: Path
    default_out_dir: Path
    ## Target -> where the app keeps that file.
    outputs: dict[Target, Path]

    def fill_for(self, target: Target) -> float:
        return self.fill_by_target.get(target.name, self.fill)


def _required(raw: dict[str, Any], key: str, source: Path) -> Any:
    if key not in raw:
        raise LogoConfigError(f"{source}: the logo config has no {key!r}")
    return raw[key]


@lru_cache(maxsize=None)
def load_config(path: Path | None = None) -> LogoConfig:
    project = get_project()
    source = path or project.logo_files().config
    raw = json.loads(source.read_text(encoding="utf-8"))
    fill = _required(raw, "fill", source)
    fill_by_target = {name: value for name, value in raw.get("fill_by_target", {}).items() if not name.startswith("_")}
    for name, value in {"fill": fill, **fill_by_target}.items():
        if name != "fill":
            target(name)
        if not 0 < value <= 1:
            raise LogoConfigError(f"{source}: {name} fill {value} is not in (0, 1] — the motif must stay inside each safe circle")
    return LogoConfig(
        model=_required(raw, "model", source),
        location=_required(raw, "location", source),
        key_color=color(_required(raw, "key_color", source), field="key_color"),
        background=color(_required(raw, "background", source), field="background"),
        fill=fill,
        fill_by_target=fill_by_target,
        style=raw.get("style", ""),
        master=project.root / Path(_required(raw, "master", source)).expanduser(),
        default_out_dir=project.root / Path(_required(raw, "default_out_dir", source)).expanduser(),
        outputs={
            target(name): project.root / Path(relative).expanduser()
            for name, relative in _required(raw, "targets", source).items()
            if not name.startswith("_")
        },
    )


__all__ = [
    "LAYERS",
    "MASKS",
    "PLATFORMS_PATH",
    "LogoConfig",
    "LogoConfigError",
    "Target",
    "color",
    "hex_color",
    "load_config",
    "target",
    "targets",
]
