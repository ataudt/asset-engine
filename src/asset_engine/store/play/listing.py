"""The Play store listing: text, screenshots, feature graphic and What's New, through one edit.

Layer: asset engine, store publishing.
Rules:
  - One edit per run. It is read first and compared with the repo; without ``execute`` it is
    deleted unwritten and the run is a plan. With ``execute`` only what differs is written, Google
    validates the edit, and it is committed. A run that fails midway deletes its edit, so nothing
    half-written is ever committed.
  - Images are compared by sha256 against what Play holds, per image class and in order. A class
    whose images are byte-identical is not touched: every replaced image goes through store review
    again (NutriSpy, Sep 2026: an upload meant for What's New put the identical images of 16 locales
    back into review). A class that differs is replaced whole, so the order is the repo's.
  - What's New belongs to a release, so it is written only against a version code, on the track that
    holds it. Without one the release notes are left as they are.
  - A listing locale Play has and the repo does not publish is left alone and named: removing a
    language is a decision taken in the console.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

from asset_engine.store.listing import PlayListing
from asset_engine.store.play.client import PlayApiError, PlayClient
from asset_engine.store.report import Report


TEXT_FIELDS = (("title", "title"), ("shortDescription", "short_description"), ("fullDescription", "full_description"))
## Google asks for this flag when the app's changes cannot go to review on commit by themselves
## (managed publishing, or a draft app): the edit is then committed and sent for review in the console.
NOT_SENT_FOR_REVIEW_HINT = "changesNotSentForReview"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _content_type(path: Path) -> str:
    return "image/png" if path.suffix.lower() == ".png" else "image/jpeg"


def _preview(text: str, width: int = 60) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


@dataclass
class _Edit:
    client: PlayClient
    id: str

    def path(self, rest: str = "") -> str:
        return f"edits/{self.id}" + (f"/{rest}" if rest else "")


def _sync_text(edit: _Edit, listing: PlayListing, live: dict | None, report: Report) -> None:
    wanted = {api: getattr(listing, attr) for api, attr in TEXT_FIELDS}
    current = {api: (live or {}).get(api, "") for api, _ in TEXT_FIELDS}
    differing = [api for api in wanted if wanted[api] != current[api]]
    if not differing:
        report.ok(f"[{listing.tag}] title, short and full description")
        return
    for api in differing:
        if live is None:
            report.change(f"[{listing.tag}] {api}: new — {_preview(wanted[api])}")
        else:
            report.change(
                f"[{listing.tag}] {api}: {len(current[api])} -> {len(wanted[api])} chars — {_preview(wanted[api])}"
            )
    if report.execute:
        body = {"language": listing.tag, **wanted}
        if live and live.get("video"):
            body["video"] = live["video"]
        edit.client.put(edit.path(f"listings/{listing.tag}"), body)


def _sync_images(edit: _Edit, listing: PlayListing, report: Report) -> None:
    for image_type, paths in listing.images.items():
        wanted = [sha256(path) for path in paths]
        live = edit.client.get(edit.path(f"listings/{listing.tag}/{image_type}")).get("images", [])
        current = [image.get("sha256", "") for image in live]
        if wanted == current:
            report.ok(f"[{listing.tag}] {image_type}: {len(wanted)} image(s) identical")
            continue
        changed = sum(1 for index, digest in enumerate(wanted) if index >= len(current) or current[index] != digest)
        report.change(
            f"[{listing.tag}] {image_type}: replace {len(current)} with {len(wanted)} image(s) "
            f"({changed} new or changed)"
        )
        if not report.execute:
            continue
        if live:
            edit.client.delete(edit.path(f"listings/{listing.tag}/{image_type}"))
        for path in paths:
            edit.client.request(
                "POST",
                edit.path(f"listings/{listing.tag}/{image_type}"),
                params={"uploadType": "media"},
                data=path.read_bytes(),
                content_type=_content_type(path),
                upload=True,
            )


def _sync_release_notes(
    edit: _Edit, listings: list[PlayListing], *, track: str, version_code: int, report: Report
) -> None:
    track_body = edit.client.get(edit.path(f"tracks/{track}"))
    releases = track_body.get("releases", [])
    release = next((r for r in releases if str(version_code) in (r.get("versionCodes") or [])), None)
    if release is None:
        raise PlayApiError(
            f"no release with version code {version_code} on track {track!r} — What's New belongs to a "
            f"release; name the track the build sits on",
            404,
        )
    notes = {note["language"]: note["text"] for note in release.get("releaseNotes") or []}
    dirty = False
    for listing in listings:
        if listing.release_notes is None:
            report.note(f"[{listing.tag}] no What's New block in update/google.md — release notes left as they are")
            continue
        if notes.get(listing.tag) == listing.release_notes:
            report.ok(f"[{listing.tag}] What's New on {track} {version_code}")
            continue
        report.change(f"[{listing.tag}] What's New on {track} {version_code}: {_preview(listing.release_notes)}")
        notes[listing.tag] = listing.release_notes
        dirty = True
    if dirty and report.execute:
        release["releaseNotes"] = [{"language": tag, "text": text} for tag, text in notes.items()]
        edit.client.put(edit.path(f"tracks/{track}"), track_body)


def _commit(edit: _Edit, report: Report) -> None:
    edit.client.post(edit.path() + ":validate")
    try:
        edit.client.post(edit.path() + ":commit")
    except PlayApiError as error:
        if NOT_SENT_FOR_REVIEW_HINT not in str(error):
            raise
        edit.client.post(edit.path() + ":commit", changesNotSentForReview="true")
        report.note("committed without sending for review: send the changes for review in the Play Console")


def upload_listing(
    client: PlayClient,
    listings: list[PlayListing],
    *,
    execute: bool,
    track: str | None = None,
    version_code: int | None = None,
    known_tags: set[str] | None = None,
) -> Report:
    """Plan (by default) or write the Play listing of ``listings`` in one edit.

    ``known_tags`` are every language the app publishes; a Play language outside them is named, one
    left out of this run (``--language``) is not."""
    report = Report(execute=execute)
    report.section(f"Play listing of {client.package_name}")
    edit = _Edit(client, client.post("edits")["id"])
    committed = False
    try:
        live = {row["language"]: row for row in client.get(edit.path("listings")).get("listings", [])}
        published = known_tags if known_tags is not None else {listing.tag for listing in listings}
        for tag in sorted(set(live) - published):
            report.note(f"[{tag}] on Play but not published from the repo — left as it is")
        for listing in listings:
            _sync_text(edit, listing, live.get(listing.tag), report)
            _sync_images(edit, listing, report)
        if version_code is not None:
            _sync_release_notes(edit, listings, track=track or "production", version_code=version_code, report=report)
        if execute and report.changes:
            _commit(edit, report)
            committed = True
    finally:
        if not committed:
            try:
                client.delete(edit.path())
            except PlayApiError:
                ## The edit expires on its own; a failed cleanup must not hide the error that got here.
                pass
    print(report.summary())
    return report


__all__ = ["sha256", "upload_listing"]
