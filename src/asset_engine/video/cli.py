"""CLI for short vertical videos. Self-contained: no app, no ``AssetProject``; every input is given here.

Usage:
    python -m asset_engine.video render RAW.mp4 cues.txt --out-dir DIR --name "kalorien app" \\
        --mirror --trim auto --fix fixes.txt
    python -m asset_engine.video sidecars RAW.mp4 cues.txt -o DIR/2026-11-16_kalorien-app.mp4 \\
        --trim auto --fix fixes.txt          # only the files beside an existing render

The video is named ``<recording date>_<name>.mp4`` (``JJJJ-MM-TT``, the camera's own date); ``-o``
names it freely instead. Beside it:
    <stem>.srt             the captions as a caption track (YouTube takes it as a file)
    <stem>.cover.jpg       a cover frame: the middle of the first picture, or --cover-at
    <stem>.transcript.txt  what is said and shown, on the cut's clock: the basis for the upload text

The words are transcribed once into ``--words`` (default: ``words.json`` beside the cue file) and
read from there afterwards. ``--fix`` corrects them, one ``wrong => right`` per line.
Cue file format: see ``asset_engine.video.render.parse_cues``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re

from asset_engine.printing.report import Run
from asset_engine.video.captions import (CaptionError, Word, apply_fixes, captions_srt, join_hyphens, shift,
                                         transcribe, transcript)
from asset_engine.video.render import (Cue, Job, VideoError, cut_range, parse_cues, probe, recording_date, render,
                                       shift_cues, still)

UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def output_path(args: argparse.Namespace, video: Path) -> Path:
    """``-o`` as given, else ``<out-dir>/<recording date>_<name>.mp4``."""
    if args.out:
        return Path(args.out).expanduser()
    if not (args.out_dir and args.name):
        raise VideoError("give --out-dir and --name (1-3 words), or -o")
    words = re.findall(r"[\w]+", args.name.lower().translate(UMLAUTS))
    if not 1 <= len(words) <= 3:
        raise VideoError(f"--name {args.name!r}: 1-3 words")
    return Path(args.out_dir).expanduser() / f"{recording_date(video).isoformat()}_{'-'.join(words)}.mp4"


@dataclass
class Prepared:
    video: Path
    cues: list[Cue]
    warnings: list[str]
    cut: tuple[float, float]
    trimmed: bool
    words: list[Word] | None


def prepare(args: argparse.Namespace) -> Prepared:
    """Cues, cut and the corrected words, all in the raw clip's time."""
    video = Path(args.video).expanduser()
    cues_path = Path(args.cues).expanduser() if args.cues else None
    duration, has_audio = probe(video)
    cues, warnings = parse_cues(cues_path, duration) if cues_path else ([], [])
    t0, t1 = cut_range(args.trim, video, duration, pad_start=args.pad_start, pad_end=args.pad_end)
    if args.trim:
        print(f"     cut {t0:.2f}–{t1:.2f} s of {duration:.2f} s")
    words = None
    if has_audio:
        cache = Path(args.words).expanduser() if args.words else (cues_path.parent / "words.json" if cues_path else None)
        words = join_hyphens(transcribe(video, model=args.whisper_model, cache=cache))
        if args.fix:
            words = apply_fixes(words, Path(args.fix).expanduser())
    return Prepared(video, cues, warnings, (t0, t1), bool(args.trim), words)


def sidecars(out: Path, p: Prepared, *, cover_at: float | None) -> None:
    """The files beside a rendered clip: captions track, cover frame, transcript."""
    t0, t1 = p.cut
    duration = t1 - t0
    cues = shift_cues(p.cues, t0, t1)
    words = shift(p.words or [], t0, t1)

    srt = out.with_suffix(".srt")
    captions_srt(words, srt)
    print(f"ok   captions: {srt}")

    if cover_at is None:
        cover_at = (cues[0].start + cues[0].end) / 2 if cues else duration / 3
    cover = out.with_name(f"{out.stem}.cover.jpg")
    still(out, cover_at, cover)
    print(f"ok   cover: {cover} (at {cover_at:.2f} s)")

    lines = [f"{out.name} · {duration:.2f} s · 1080×1920",
             f"source {p.video.name}" + (f", cut {t0:.2f}–{t1:.2f} s" if p.trimmed else ""),
             f"cover at {cover_at:.2f} s", ""]
    if cues:
        lines += ["Pictures:"] + [f"  {c.start:5.2f}–{c.end:5.2f} s  {c.image.name}" for c in cues] + [""]
    lines += ["Transcript:", transcript(words), ""]
    text = out.with_name(f"{out.stem}.transcript.txt")
    text.write_text("\n".join(lines), encoding="utf-8")
    print(f"ok   transcript: {text}")


def _cmd_render(args: argparse.Namespace) -> int:
    run = Run()
    p = prepare(args)
    out = output_path(args, p.video)
    if out.exists() and not args.overwrite:
        raise VideoError(f"{out} exists; --overwrite renders over it")
    result = render(Job(
        video=p.video, out=out, cues=p.cues, words=None if args.no_captions else p.words, fit=args.fit,
        mirror=args.mirror, trim=p.cut if p.trimmed else None, pop_sound=not args.no_pop_sound,
        pop_volume=args.pop_volume, audio_fix=not args.no_audio_fix,
    ))
    print(f"ok   video: {out} ({result.duration:.2f} s"
          + (f", {result.loudness:.1f} LUFS)" if result.loudness is not None else ")"))
    run.warn("video", p.warnings + result.warnings)
    sidecars(out, p, cover_at=args.cover_at)
    return run.summary()


def _cmd_sidecars(args: argparse.Namespace) -> int:
    p = prepare(args)
    out = output_path(args, p.video)
    if not out.is_file():
        raise VideoError(f"no rendered clip at {out}")
    sidecars(out, p, cover_at=args.cover_at)
    return 0


def _inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("video", help="the raw clip")
    parser.add_argument("cues", nargs="?", help="cue file: which picture pops in when")
    parser.add_argument("-o", "--out", help="the rendered .mp4, named freely")
    parser.add_argument("--out-dir", help="directory for <recording date>_<name>.mp4")
    parser.add_argument("--name", help="1-3 words for the content, e.g. 'kalorien app'")
    parser.add_argument("--trim", help="'auto' (at the speech) or START-END in the raw clip's time")
    parser.add_argument("--pad-start", type=float, default=0.2, help="seconds kept before the first sound (default 0.2)")
    parser.add_argument("--pad-end", type=float, default=0.35, help="seconds kept after the last sound (default 0.35)")
    parser.add_argument("--words", help="the transcript cache (default: words.json beside the cue file)")
    parser.add_argument("--fix", help="caption corrections, one 'wrong => right' per line")
    parser.add_argument("--whisper-model", default="medium")
    parser.add_argument("--cover-at", type=float, help="cover frame, seconds into the rendered clip")


def build_parser(*, prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Render short vertical videos.")
    commands = parser.add_subparsers(dest="command", required=True)

    r = commands.add_parser("render", help="pictures on cue, captions, cleaned-up voice; plus the files beside it")
    _inputs(r)
    r.add_argument("--mirror", action="store_true", help="mirror the camera image (a selfie camera's)")
    r.add_argument("--fit", choices=["blur", "crop"], default="blur", help="a landscape clip on a blurred copy, or cropped")
    r.add_argument("--no-captions", action="store_true", help="burn in no captions (the .srt is still written)")
    r.add_argument("--no-pop-sound", action="store_true")
    r.add_argument("--pop-volume", type=float, default=0.35)
    r.add_argument("--no-audio-fix", action="store_true", help="leave the voice as recorded")
    r.add_argument("--overwrite", action="store_true", help="render over an existing file")
    r.set_defaults(func=_cmd_render)

    s = commands.add_parser("sidecars", help="only the files beside an existing render (same inputs as render)")
    _inputs(s)
    s.set_defaults(func=_cmd_sidecars)
    return parser


def main(argv: list[str] | None = None, *, prog: str = "python -m asset_engine.video") -> int:
    args = build_parser(prog=prog).parse_args(argv)
    try:
        return args.func(args)
    except (VideoError, CaptionError) as error:
        print(f"FAIL {error}")
        return 1


__all__ = ["build_parser", "main", "output_path", "prepare", "sidecars"]
