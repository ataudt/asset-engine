"""Words with times from speech, corrected, and drawn as captions.

Layer: asset engine.
Rules:
  - Transcription runs once per clip: the words go to a cache file, and every later render reads
    them from there, so a render is a function of that file and not of the speech model.
  - Times are the raw clip's. Moving them onto the cut's clock is the renderer's job (``cuts.Timeline``).
  - Hyphenated words are joined before corrections apply, so a correction is written the way the
    word is shown.
  - A caption group that would reach past the frame's edge is drawn narrower, not cut off: German
    compounds ("Datenschutzeinstellungen") are longer than the frame at the captions' size. The width
    is estimated from the letter count, since the face is the player's and cannot be measured here.
  - ``faster-whisper`` is the ``video`` extra and is imported only when a clip has no cache yet.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re


CAPTION_SIZE = 84
## Left of the frame's edge, and left of the platforms' button column.
CAPTION_MARGIN_L, CAPTION_MARGIN_R = 80, 180
## An upper-case letter of Arial Black is about this wide, in units of the size.
CAPTION_LETTER_EM = 0.54
## What stays clear at the frame's edge.
CAPTION_EDGE = 30


class CaptionError(RuntimeError):
    """The words cannot be read, transcribed or corrected as given."""


@dataclass(frozen=True)
class Word:
    word: str
    start: float
    end: float


def transcribe(video: Path, *, model: str, cache: Path | None) -> list[Word]:
    """Every spoken word with its times; read from ``cache`` when it exists, else written there."""
    if cache is not None and cache.is_file():
        return [Word(**w) for w in json.loads(cache.read_text(encoding="utf-8"))]
    try:
        from faster_whisper import WhisperModel
    except ModuleNotFoundError as error:
        raise CaptionError("transcribing needs faster-whisper: pip install -e '../asset-engine[video]'") from error
    print(f"     transcribing with whisper {model} (once; cached afterwards) ...", flush=True)
    segments, info = WhisperModel(model, device="cpu", compute_type="int8").transcribe(
        str(video), word_timestamps=True, vad_filter=True,
    )
    words = [Word(w.word.strip(), w.start, w.end) for segment in segments for w in segment.words]
    print(f"     language {info.language}, {len(words)} words")
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps([asdict(w) for w in words], ensure_ascii=False, indent=1), encoding="utf-8")
    return words


def join_hyphens(words: list[Word]) -> list[Word]:
    """Whisper splits a hyphenated word ("KI" + "-Fotoanalyse"); make it one word again."""
    joined: list[Word] = []
    for w in words:
        if joined and w.word.startswith("-"):
            joined[-1] = Word(joined[-1].word + w.word, joined[-1].start, w.end)
        else:
            joined.append(w)
    return joined


def _bare(s: str) -> str:
    return re.sub(r"[^\w-]", "", s).lower()


def apply_fixes(words: list[Word], fixes_path: Path) -> list[Word]:
    """Replace word sequences per ``wrong => right`` line (``#`` starts a comment, case ignored).
    The old sequence's time is spread evenly over the new words; trailing punctuation is kept."""
    fixes = []
    for lineno, line in enumerate(fixes_path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if "=>" not in line:
            raise CaptionError(f"{fixes_path}:{lineno}: no '=>' in {line!r}")
        wrong, right = (part.split() for part in line.split("=>", 1))
        fixes.append(([_bare(x) for x in wrong], right))
    out, i = [], 0
    while i < len(words):
        for wrong, right in fixes:
            seq = words[i:i + len(wrong)]
            if len(seq) == len(wrong) and [_bare(w.word) for w in seq] == wrong:
                t0, t1 = seq[0].start, seq[-1].end
                tail = re.search(r"[.,!?;:]*$", seq[-1].word).group()
                step = (t1 - t0) / max(1, len(right))
                for k, r in enumerate(right):
                    out.append(Word(r + (tail if k == len(right) - 1 else ""), t0 + k * step, t0 + (k + 1) * step))
                i += len(wrong)
                break
        else:
            out.append(words[i])
            i += 1
    return out


def groups(words: list[Word]) -> list[list[Word]]:
    """Caption groups: at most 3 words or about 18 characters, broken at punctuation and pauses."""
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= 3 or sum(len(x.word) for x in cur) + len(w.word) > 18
                    or w.start - cur[-1].end > 0.4 or cur[-1].word[-1:] in ".,!?;:"):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def _spans(grouped: list[list[Word]]) -> list[tuple[float, float]]:
    """When each group shows: at least 0.35 s, and a gap under 0.7 s to the next is bridged so the
    captions do not flicker."""
    spans = []
    for n, g in enumerate(grouped):
        start, end = g[0].start, max(g[-1].end, g[0].start + 0.35)
        if n + 1 < len(grouped) and grouped[n + 1][0].start - end < 0.7:
            end = grouped[n + 1][0].start
        spans.append((start, end))
    return spans


def captions_ass(words: list[Word], out: Path, *, width: int, height: int, baseline: int) -> None:
    """Karaoke captions (ASS): the spoken word yellow, the rest white, each group popping in."""

    def ts(t: float) -> str:
        cs = round(t * 100)
        return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"

    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}", "WrapStyle: 2", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        ## Primary = the word being spoken (yellow), secondary = not spoken yet (white).
        f"Style: Cap,Arial Black,{CAPTION_SIZE},&H0000E6FF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,3,"
        f"2,{CAPTION_MARGIN_L},{CAPTION_MARGIN_R},{height - baseline},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    grouped = groups(words)
    ## The captions are centred between the margins, so the nearer edge of the frame limits them.
    centre = (CAPTION_MARGIN_L + width - CAPTION_MARGIN_R) / 2
    room = 2 * (min(centre, width - centre) - CAPTION_EDGE)
    for g, (start, end) in zip(grouped, _spans(grouped)):
        text = " ".join(f"{{\\k{max(1, round((w.end - w.start) * 100))}}}{w.word.upper()}" for w in g)
        letters = sum(len(w.word) for w in g) + len(g) - 1
        fit = min(1.0, room / (letters * CAPTION_SIZE * CAPTION_LETTER_EM))
        a, b, c = (round(x * fit) for x in (80, 105, 100))
        pop = f"{{\\fscx{a}\\fscy{a}\\t(0,90,\\fscx{b}\\fscy{b})\\t(90,160,\\fscx{c}\\fscy{c})}}"
        lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Cap,,0,0,0,,{pop}{text}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def captions_srt(words: list[Word], out: Path) -> None:
    """Plain subtitles (SRT) in the same groups and times, for a platform's own caption track."""

    def ts(t: float) -> str:
        ms = round(t * 1000)
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    grouped = groups(words)
    blocks = [
        f"{n}\n{ts(start)} --> {ts(end)}\n{' '.join(w.word for w in g)}\n"
        for n, (g, (start, end)) in enumerate(zip(grouped, _spans(grouped)), 1)
    ]
    out.write_text("\n".join(blocks), encoding="utf-8")


def transcript(words: list[Word]) -> str:
    return " ".join(w.word for w in words)


__all__ = ["CaptionError", "Word", "apply_fixes", "captions_ass", "captions_srt", "groups", "join_hyphens",
           "transcribe", "transcript"]
