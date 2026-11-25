"""What a publishing run found and did, one printed line per setting.

Layer: asset engine, store upload.
Rules:
  - Every publish step reads the live state first and plans; it writes only with ``execute``, and
    then only what differs. The report is that plan: ``ok`` for what already matches, ``change`` for
    what is planned or written, ``note`` for what differs but is deliberately left to a person (a
    period that cannot change, a live price a run must not overwrite).
  - A run that writes nothing says so: ``changes == 0`` is the acceptance test of a migration, the
    proof that the source in the repo describes the store as it stands.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Report:
    """The plan of one run, or what it did when ``execute`` is set."""

    execute: bool
    lines: list[str] = field(default_factory=list)
    changes: int = 0
    notes: int = 0

    def section(self, title: str) -> None:
        self._print(title)

    def ok(self, line: str) -> None:
        self._print(f"  ok      {line}")

    def change(self, line: str) -> None:
        self.changes += 1
        self._print(f"  {'set' if self.execute else 'plan'}    {line}")

    def note(self, line: str) -> None:
        self.notes += 1
        self._print(f"  note    {line}")

    def summary(self) -> str:
        if not self.changes:
            return "nothing to change: the store matches the repo"
        if self.execute:
            return f"{self.changes} change(s) written"
        return f"{self.changes} change(s) planned — nothing was written. Re-run with --apply to write them."

    def _print(self, line: str) -> None:
        self.lines.append(line)
        print(line, flush=True)


__all__ = ["Report"]
