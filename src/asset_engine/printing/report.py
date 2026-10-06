"""How a build speaks: it always finishes, and says loudly what is wrong with what it wrote.

Layer: asset engine.
Rules:
  - A judgement never stops a build. A rule finding or type that overflows its band is drawn as it
    falls and written; the build prints it in a ``loud`` block. An output that was withheld left the
    previous file in its place, and that file was then judged as if it were the current code.
  - What cannot be drawn at all (a missing capture, picture or badge, a broken config) fails loudly
    and ``clear_outputs`` removes the unit's previous files, so nothing stale is left looking current.
  - Every CLI ends with ``Run.summary``: one line per unit that warned or failed, after all the
    per-unit output has scrolled by.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

BAR = "!" * 78


def loud(label: str, lines: list[str], *, headline: str) -> None:
    """Print ``lines`` about ``label`` inside a block of ``!`` that cannot be scrolled past unseen."""
    print(BAR)
    print(f"!! {label} — {headline}")
    for line in lines:
        print(f"!!   {line}")
    print(BAR, flush=True)


def clear_outputs(paths: list[Path]) -> list[Path]:
    """Delete whichever of ``paths`` exist; returns the ones removed."""
    removed = [path for path in paths if path.is_file()]
    for path in removed:
        path.unlink()
    return removed


@dataclass
class Run:
    """The units of one build that warned or failed, for the closing summary."""

    warned: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def warn(self, label: str, lines: list[str]) -> None:
        """A unit was written with something wrong in it."""
        if lines:
            self.warned.append(label)
            loud(label, lines, headline=f"written anyway, {len(lines)} warning(s):")

    def fail(self, label: str, message: str, removed: list[Path] = ()) -> None:
        """A unit could not be drawn; ``removed`` are its previous files, now deleted."""
        self.failed.append(label)
        lines = [message]
        if removed:
            lines.append(f"removed the previous output so it is not mistaken for this build: {', '.join(p.name for p in removed)}")
        loud(label, lines, headline="NOT WRITTEN:")

    def summary(self) -> int:
        """Print the closing lines; the exit code is 1 only when something could not be drawn."""
        if self.warned:
            print(f"WARNED  {len(self.warned)}: {', '.join(self.warned)}")
        if self.failed:
            print(f"FAILED  {len(self.failed)}: {', '.join(self.failed)}")
        return 1 if self.failed else 0


__all__ = ["BAR", "Run", "clear_outputs", "loud"]
