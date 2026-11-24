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

``--denoise`` separates the voice from wind and other changing noise with a speech model, once,
into ``voice.wav`` beside the cue file; the transcript, ``--trim auto`` and the render then all read
that file. ``--cover-title`` sets a headline on the cover frame (``--cover-font`` names the face).

``--cuts`` names the removals (``cuts.parse_cuts``: one ``START - END`` per line, the raw clip's time)
and ``--tighten`` shortens every pause to that many seconds; ``words`` prints the transcript with
times and pauses, which is what a removal is written from. ``--layout stack`` puts ``--screen`` (a
second clip, tied to the speech by ``--screen-track``) above the speaker, ``--layout pip`` shows the
speaker in a bubble over it. ``--title`` sets a headline across the top (``--title-font`` its face).

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
from asset_engine.video.captions import (CaptionError, Word, apply_fixes, captions_srt, join_hyphens, transcribe,
                                         transcript)
from asset_engine.video.cuts import CutError, Timeline, parse_cuts, tighten
from asset_engine.video.denoise import clean_voice
from asset_engine.video.layout import LAYOUTS, Layout, LayoutError, ScreenSpan, parse_screen_track
from asset_engine.video.render import (FPS, POSITIONS, Cue, Job, VideoError, cover, cut_range, parse_cues, probe,
                                       recording_date, render, silences, still)

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
    ## The kept segments: ``cut`` without the removals and the insides of long pauses.
    timeline: Timeline
    ## The cleaned voice (``--denoise``), on the raw clip's clock.
    voice: Path | None = None
    mirror: bool = False


def _beside_cues(args: argparse.Namespace, given: str | None, name: str) -> Path | None:
    if given:
        return Path(given).expanduser()
    return Path(args.cues).expanduser().parent / name if args.cues else None


def prepare(args: argparse.Namespace) -> Prepared:
    """Cues, cut and the corrected words, all in the raw clip's time."""
    video = Path(args.video).expanduser()
    cues_path = Path(args.cues).expanduser() if args.cues else None
    duration, has_audio = probe(video)
    cues, warnings = parse_cues(cues_path, duration) if cues_path else ([], [])
    voice = None
    if args.denoise and has_audio:
        cache = _beside_cues(args, args.voice, "voice.wav")
        if cache is None:
            raise VideoError("--denoise: give --voice (the cache file) or a cue file to put voice.wav beside")
        model = Path(args.denoise_model).expanduser() if args.denoise_model else None
        voice = clean_voice(video, cache=cache, model=model)
    ## The speech bounds and the words come from the cleaned voice where there is one: same clock.
    heard = voice or video
    t0, t1 = cut_range(args.trim, heard, duration, pad_start=args.pad_start, pad_end=args.pad_end)
    if args.trim:
        print(f"     cut {t0:.2f}–{t1:.2f} s of {duration:.2f} s")
    removed = parse_cuts(Path(args.cuts).expanduser()) if args.cuts else []
    if args.tighten is not None and has_audio:
        removed = tighten(silences(heard), removed, args.tighten)
    timeline = Timeline.of((t0, t1), removed, fps=FPS)
    if removed:
        print(f"     {len(timeline.segments)} segments kept, {timeline.duration:.2f} s")
    words = None
    if has_audio:
        cache = _beside_cues(args, args.words, "words.json")
        words = join_hyphens(transcribe(heard, model=args.whisper_model, cache=cache))
        if args.fix:
            words = apply_fixes(words, Path(args.fix).expanduser())
    return Prepared(video, cues, warnings, (t0, t1), bool(args.trim), words, timeline, voice, args.mirror)


def sidecars(out: Path, p: Prepared, *, cover_at: float | None, cover_title: str | None = None,
             cover_font: str | None = None) -> None:
    """The files beside a rendered clip: captions track, cover frame, transcript."""
    t0, t1 = p.cut
    duration = p.timeline.duration
    cues = p.timeline.clip(p.cues)
    words = p.timeline.clip(p.words or [])

    srt = out.with_suffix(".srt")
    captions_srt(words, srt)
    print(f"ok   captions: {srt}")

    if cover_at is None:
        cover_at = (cues[0].start + cues[0].end) / 2 if cues else duration / 3
    cover_path = out.with_name(f"{out.stem}.cover.jpg")
    if cover_title:
        if not cover_font:
            raise VideoError("--cover-title needs --cover-font (the engine holds no face of its own)")
        ## From the raw clip, on its clock: the rendered frame already carries a caption.
        ## Only before the first removal is the cut's clock the raw clip's, shifted.
        at = min(t0 + cover_at, p.timeline.segments[0][1])
        cover(p.video, at, cover_path, title=cover_title, font=Path(cover_font).expanduser(), mirror=p.mirror)
    else:
        still(out, cover_at, cover_path)
    print(f"ok   cover: {cover_path} (at {cover_at:.2f} s)")

    lines = [f"{out.name} · {duration:.2f} s · 1080×1920",
             f"source {p.video.name}" + (f", cut {t0:.2f}–{t1:.2f} s" if p.trimmed else "")
             + (f", {len(p.timeline.segments)} segments" if len(p.timeline.segments) > 1 else ""),
             f"cover at {cover_at:.2f} s", ""]
    if cues:
        lines += ["Pictures:"] + [f"  {c.start:5.2f}–{c.end:5.2f} s  {c.image.name}" for c in cues] + [""]
    lines += ["Transcript:", transcript(words), ""]
    text = out.with_name(f"{out.stem}.transcript.txt")
    text.write_text("\n".join(lines), encoding="utf-8")
    print(f"ok   transcript: {text}")


def _anchor(spec: str | None, default: tuple[float, float]) -> tuple[float, float]:
    """A position word (``oben``, ``unten-links``) or ``X%,Y%``."""
    if spec is None:
        return default
    if spec.lower() in POSITIONS:
        return POSITIONS[spec.lower()]
    try:
        x, y = (float(part.rstrip("%")) / 100 for part in spec.split(","))
    except ValueError as error:
        raise VideoError(f"{spec!r}: a position word or X%,Y%") from error
    return x, y


def _layout(args: argparse.Namespace) -> tuple[Layout, Path | None, list[ScreenSpan]]:
    default = Layout()
    layout = Layout(kind=args.layout, split=args.split, face_anchor=_anchor(args.face_anchor, default.face_anchor),
                    bubble_anchor=_anchor(args.bubble_anchor, default.bubble_anchor), punch=args.punch)
    if layout.kind == "full":
        return layout, None, []
    if not args.screen:
        raise VideoError(f"--layout {layout.kind} needs --screen (the second clip)")
    screen = Path(args.screen).expanduser()
    if not screen.is_file():
        raise VideoError(f"no second clip at {screen}")
    spans = parse_screen_track(Path(args.screen_track).expanduser()) if args.screen_track else []
    return layout, screen, spans


def _cmd_render(args: argparse.Namespace) -> int:
    run = Run()
    p = prepare(args)
    out = output_path(args, p.video)
    if out.exists() and not args.overwrite:
        raise VideoError(f"{out} exists; --overwrite renders over it")
    layout, screen, spans = _layout(args)
    result = render(Job(
        video=p.video, out=out, cues=p.cues, words=None if args.no_captions else p.words, fit=args.fit,
        mirror=args.mirror, trim=p.cut if p.trimmed else None, pop_sound=not args.no_pop_sound,
        pop_volume=args.pop_volume, audio_fix=not args.no_audio_fix, voice=p.voice,
        keep=p.timeline if len(p.timeline.segments) > 1 else None, layout=layout, screen=screen, screen_spans=spans,
        title=args.title, title_font=Path(args.title_font).expanduser() if args.title_font else None,
        title_until=args.title_until,
    ))
    print(f"ok   video: {out} ({result.duration:.2f} s"
          + (f", {result.loudness:.1f} LUFS)" if result.loudness is not None else ")"))
    run.warn("video", p.warnings + result.warnings)
    sidecars(out, p, cover_at=args.cover_at, cover_title=args.cover_title, cover_font=args.cover_font)
    return run.summary()


def _cmd_sidecars(args: argparse.Namespace) -> int:
    p = prepare(args)
    out = output_path(args, p.video)
    if not out.is_file():
        raise VideoError(f"no rendered clip at {out}")
    sidecars(out, p, cover_at=args.cover_at, cover_title=args.cover_title, cover_font=args.cover_font)
    return 0


def _cmd_words(args: argparse.Namespace) -> int:
    """The transcript in the raw clip's time, a line per phrase, with the pause before it."""
    p = prepare(args)
    line: list[Word] = []
    last_end = 0.0

    def flush(pause: float) -> None:
        if line:
            print(f"{line[0].start:7.2f}–{line[-1].end:6.2f}  {transcript(line)}")
        if pause >= 0.3:
            print(f"{'':16}(pause {pause:.2f} s)")

    for w in p.words or []:
        pause = w.start - last_end
        if line and (pause >= 0.3 or line[-1].word[-1:] in ".!?"):
            flush(pause)
            line = []
        line.append(w)
        last_end = w.end
    flush(0.0)
    return 0


def _inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("video", help="the raw clip")
    parser.add_argument("cues", nargs="?", help="cue file: which picture pops in when")
    parser.add_argument("-o", "--out", help="the rendered .mp4, named freely")
    parser.add_argument("--out-dir", help="directory for <recording date>_<name>.mp4")
    parser.add_argument("--name", help="1-3 words for the content, e.g. 'kalorien app'")
    parser.add_argument("--trim", help="'auto' (at the speech) or START-END in the raw clip's time")
    parser.add_argument("--cuts", help="removals, one 'START - END' per line in the raw clip's time")
    parser.add_argument("--tighten", type=float, nargs="?", const=0.35,
                        help="shorten every pause to this many seconds (0.35 when given without a value)")
    parser.add_argument("--pad-start", type=float, default=0.2, help="seconds kept before the first sound (default 0.2)")
    parser.add_argument("--pad-end", type=float, default=0.35, help="seconds kept after the last sound (default 0.35)")
    parser.add_argument("--words", help="the transcript cache (default: words.json beside the cue file)")
    parser.add_argument("--fix", help="caption corrections, one 'wrong => right' per line")
    parser.add_argument("--whisper-model", default="medium")
    parser.add_argument("--cover-at", type=float, help="cover frame, seconds into the rendered clip")
    parser.add_argument("--mirror", action="store_true", help="mirror the camera image (a selfie camera's)")
    parser.add_argument("--cover-title", help="a headline set across the top of the cover")
    parser.add_argument("--cover-font", help="the face for --cover-title (.ttf/.otf)")
    parser.add_argument("--denoise", action="store_true",
                        help="separate the voice from wind and other changing noise (deep-filter), cached")
    parser.add_argument("--voice", help="the cleaned-voice cache (default: voice.wav beside the cue file)")
    parser.add_argument("--denoise-model", help="DeepFilterNet model .tar.gz (default: DeepFilterNet3 in the venv)")


def build_parser(*, prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Render short vertical videos.")
    commands = parser.add_subparsers(dest="command", required=True)

    r = commands.add_parser("render", help="pictures on cue, captions, cleaned-up voice; plus the files beside it")
    _inputs(r)
    r.add_argument("--fit", choices=["blur", "crop"], default="blur", help="a landscape clip on a blurred copy, or cropped")
    r.add_argument("--no-captions", action="store_true", help="burn in no captions (the .srt is still written)")
    r.add_argument("--no-pop-sound", action="store_true")
    r.add_argument("--pop-volume", type=float, default=0.35)
    r.add_argument("--no-audio-fix", action="store_true", help="leave the voice as recorded")
    r.add_argument("--overwrite", action="store_true", help="render over an existing file")
    r.add_argument("--layout", choices=LAYOUTS, default="full",
                   help="full: the speaker alone; stack: --screen above the speaker; pip: the speaker in a bubble over it")
    r.add_argument("--screen", help="the second clip (a screen recording), shown without its sound")
    r.add_argument("--screen-track", help="which stretch of the second clip shows when (layout.parse_screen_track)")
    r.add_argument("--split", type=float, default=0.6, help="stack: the upper field's share of the height (default 0.6)")
    r.add_argument("--face-anchor", help="which part of the speaker stays in the field: a position word or X%%,Y%%")
    r.add_argument("--bubble-anchor", help="pip: where the bubble sits in the safe zone (default: left, above the captions)")
    r.add_argument("--punch", type=float, default=0.0, help="enlarge every second segment by this share, e.g. 0.08")
    r.add_argument("--title", help="a headline across the top of the frame")
    r.add_argument("--title-font", help="the face for --title (.ttf/.otf)")
    r.add_argument("--title-until", type=float, help="seconds the headline stays (default: throughout)")
    r.set_defaults(func=_cmd_render)

    w = commands.add_parser("words", help="the transcript with times and pauses (same inputs as render)")
    _inputs(w)
    w.set_defaults(func=_cmd_words)

    s = commands.add_parser("sidecars", help="only the files beside an existing render (same inputs as render)")
    _inputs(s)
    s.set_defaults(func=_cmd_sidecars)
    return parser


def main(argv: list[str] | None = None, *, prog: str = "python -m asset_engine.video") -> int:
    args = build_parser(prog=prog).parse_args(argv)
    try:
        return args.func(args)
    except (VideoError, CaptionError, CutError, LayoutError) as error:
        print(f"FAIL {error}")
        return 1


__all__ = ["build_parser", "main", "output_path", "prepare", "sidecars"]
