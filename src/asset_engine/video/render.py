"""A vertical clip with pictures popping in on cue, captions and a cleaned-up voice.

Layer: asset engine.
Rules:
  - The output is 1080×1920 at 30 fps, H.264 and AAC at 48 kHz: what TikTok, Reels and Shorts take
    without re-encoding twice. A landscape clip is fitted on a blurred copy of itself, or cropped.
  - A picture and the captions stay inside the zone the platforms leave uncovered (``SAFE``): the
    tabs at the top, the like/comment/share column on the right, name, text and sound at the bottom.
  - Cue times, and the words, are the raw clip's. A trimmed cut shifts them here, so a cue file
    never needs rewriting when the cut moves.
  - An HDR clip (HLG or PQ, as iPhones record) is tone-mapped to SDR BT.709 first; the platforms
    show HDR uploads inconsistently, and untouched HLG in a BT.709 file looks washed out. An SDR clip
    goes through the same graph as before, so its render does not change.
  - A cleaned voice (``Job.voice``, see ``denoise``) replaces the clip's own sound and is read with the
    same cut; the steady-noise stage is left out then, since it only adds artefacts behind the model.
  - The voice is brought to −14 LUFS, the level the streaming platforms normalise to. The loudness
    is measured again behind the limiter and topped up, because a quiet recording loses level there.
  - ffmpeg comes from ``PATH`` or the ``imageio-ffmpeg`` wheel (the ``video`` extra), resolved on
    first use so the rest of the engine imports without it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
import json
import math
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import wave

from PIL import Image, ImageDraw, ImageFilter

from asset_engine.video.captions import Word, captions_ass
from asset_engine.video.cuts import Span, Timeline, parse_time
from asset_engine.video.layout import (VIDEO_SUFFIXES, Layout, ScreenSpan, bubble_images, fit_lines, panel, slots,
                                       title_card)

W, H, FPS = 1080, 1920, 30
## left, top, right, bottom
SAFE = (60, 160, 900, 1380)
CAPTION_BASELINE = 1300

POP_IN, POP_OUT = 0.30, 0.18
SHADOW_BLUR, SHADOW_OFFSET, SHADOW_ALPHA = 22, 14, 0.45
RADIUS = 32

POSITIONS = {
    "mitte": (0.5, 0.5), "center": (0.5, 0.5),
    "oben": (0.5, 0.0), "top": (0.5, 0.0),
    "unten": (0.5, 1.0), "bottom": (0.5, 1.0),
    "links": (0.0, 0.5), "left": (0.0, 0.5),
    "rechts": (1.0, 0.5), "right": (1.0, 0.5),
    "oben-links": (0.0, 0.0), "top-left": (0.0, 0.0),
    "oben-rechts": (1.0, 0.0), "top-right": (1.0, 0.0),
    "unten-links": (0.0, 1.0), "bottom-left": (0.0, 1.0),
    "unten-rechts": (1.0, 1.0), "bottom-right": (1.0, 1.0),
}

STEADY_NOISE = "afftdn=nr=12:nf=-40:tn=1"             # steady noise, learning its profile as it goes
VOICE_STAGES = [
    "highpass=f=80",                                   # rumble, footfall
    STEADY_NOISE,
    "equalizer=f=250:t=q:w=1.0:g=-3",                  # boxiness
    "equalizer=f=3500:t=q:w=1.2:g=2.5",                # presence
    "equalizer=f=11000:t=h:w=0.7:g=1.5",               # air
    "deesser=i=0.4",                                   # sibilants
    "acompressor=threshold=-22dB:ratio=3:attack=8:release=120:makeup=3dB",
]
VOICE_CHAIN = ",".join(VOICE_STAGES)
## Behind a speech model the noise is gone; afftdn would only chew on what is left.
CLEANED_VOICE_CHAIN = ",".join(s for s in VOICE_STAGES if s != STEADY_NOISE)
HDR_TRANSFERS = ("arib-std-b67", "smpte2084")
## Linear light, BT.709 primaries, Hable's curve, back to BT.709 gamma in TV range.
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
           "zscale=t=bt709:m=bt709:r=tv,format=yuv420p,")
## loudnorm alone stays under the target on speech: it keeps its true-peak ceiling and falls back
## to dynamic mode once peak and loudness are more than 12.5 dB apart. So: measure, add the
## difference, limit the peaks at about −1.7 dBFS, measure behind the limiter, top up.
TARGET_LUFS = -14.0
LIMITER = "alimiter=limit=0.82:attack=3:release=60:level=false"
SILENCE = "highpass=f=90,silencedetect=n=-40dB:d=0.15"


class VideoError(RuntimeError):
    """The clip cannot be rendered as given."""


@lru_cache(maxsize=1)
def ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
    except ModuleNotFoundError as error:
        raise VideoError("no ffmpeg on PATH and no imageio-ffmpeg: pip install -e '../asset-engine[video]'") from error
    return imageio_ffmpeg.get_ffmpeg_exe()


def recording_date(video: Path) -> date:
    """The local day the clip was recorded: the camera's ``creation_time`` (UTC), else the file's mtime."""
    r = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(video)], capture_output=True, text=True)
    m = re.search(r"creation_time\s*:\s*(\S+)", r.stderr)
    if m:
        try:
            return datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).astimezone().date()
        except ValueError:
            pass
    return datetime.fromtimestamp(video.stat().st_mtime).date()


def probe(video: Path) -> tuple[float, bool]:
    """Duration and whether there is an audio stream, from ffmpeg's header (the wheel has no ffprobe)."""
    r = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(video)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    if not m:
        raise VideoError(f"no readable video at {video}: {r.stderr[-300:]}")
    h, mi, se = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(se), bool(re.search(r"Stream #.*: Audio:", r.stderr))


def transfer(video: Path) -> str | None:
    """The video stream's transfer function as ffmpeg names it (``arib-std-b67`` = HLG), if stated."""
    r = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(video)], capture_output=True, text=True)
    m = re.search(r"Stream #.*: Video: .*?\((?:tv|pc), [\w-]+/[\w-]+/([\w-]+)", r.stderr)
    return m.group(1) if m else None


# ---------------------------------------------------------------- cues

@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    image: Path
    ## Where in the safe zone, 0..1 per axis, of the room left beside the picture.
    anchor: tuple[float, float]
    width_frac: float
    pop_sound: bool
    ## Fills the frame for its time (a cutaway) instead of popping in as a card. A video always does.
    full: bool = False


def parse_cues(path: Path, duration: float) -> tuple[list[Cue], list[str]]:
    """The cue file, one picture per line (``#`` starts a comment; picture paths relative to the file):

        00:03.2   logo.png        oben-rechts   2s
        00:07.5   screenshot.png  mitte         bis 00:10   w=60%
        12.0      foto.jpg        x=30% y=40%   1.5s        pop=aus
        00:15     demo.mp4        voll          bis 00:21

    ``voll`` (``full``) fills the frame for that time, a cutaway; a video is always shown that way,
    without its sound. Returns the cues and the warnings about them."""
    cues, warnings = [], []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        toks = line.split()
        try:
            start = parse_time(toks[0])
            image = (path.parent / toks[1]).resolve()
            anchor, width, end, pop = (0.5, 0.5), 0.45, start + 2.0, True
            full = image.suffix.lower() in VIDEO_SUFFIXES
            ax = ay = None
            i = 2
            while i < len(toks):
                t = toks[i].lower()
                if t in POSITIONS:
                    anchor = POSITIONS[t]
                elif t in ("voll", "full"):
                    full = True
                elif t.startswith("x="):
                    ax = float(t[2:].rstrip("%")) / 100
                elif t.startswith("y="):
                    ay = float(t[2:].rstrip("%")) / 100
                elif t.startswith("w="):
                    width = float(t[2:].rstrip("%")) / 100
                elif t.startswith("pop="):
                    pop = t[4:] not in ("aus", "off", "nein", "no", "0")
                elif t in ("bis", "until"):
                    i += 1
                    end = parse_time(toks[i])
                elif re.fullmatch(r"[\d.]+s", t):
                    end = start + float(t[:-1])
                else:
                    raise ValueError(f"unknown field {toks[i]!r}")
                i += 1
            if ax is not None or ay is not None:
                anchor = (ax if ax is not None else anchor[0], ay if ay is not None else anchor[1])
        except (IndexError, ValueError) as error:
            raise VideoError(f"{path}:{lineno}: {error}: {raw.strip()}") from error
        if not image.is_file():
            raise VideoError(f"{path}:{lineno}: no picture at {image}")
        if end <= start:
            raise VideoError(f"{path}:{lineno}: ends before it starts")
        if start >= duration:
            warnings.append(f"{path.name}:{lineno}: starts after the clip ends ({duration:.1f} s)")
        cues.append(Cue(start, min(end, duration), image, anchor, width, pop and not full, full))
    return cues, warnings


# ---------------------------------------------------------------- the pop

def _ease_out_back(t: float, s: float = 2.2) -> float:
    t -= 1
    return t * t * ((s + 1) * t + s) + 1


def _ease_in_back(t: float, s: float = 1.7) -> float:
    return t * t * ((s + 1) * t - s)


def _card(image_path: Path, width_px: int) -> Image.Image:
    """The picture at its width, with rounded corners unless it has its own transparency, and a shadow."""
    img = Image.open(image_path).convert("RGBA")
    img = img.resize((width_px, round(img.height * width_px / img.width)), Image.LANCZOS)
    alpha = img.getchannel("A")
    if alpha.getextrema()[0] == 255:
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, *img.size), RADIUS, fill=255)
        img.putalpha(mask)
        alpha = mask
    pad = SHADOW_BLUR * 3
    canvas = Image.new("RGBA", (img.width + 2 * pad, img.height + 2 * pad + SHADOW_OFFSET), (0, 0, 0, 0))
    shadow_a = Image.new("L", canvas.size, 0)
    shadow_a.paste(alpha.point(lambda a: int(a * SHADOW_ALPHA)), (pad, pad + SHADOW_OFFSET))
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 255))
    shadow.putalpha(shadow_a.filter(ImageFilter.GaussianBlur(SHADOW_BLUR)))
    canvas.alpha_composite(shadow)
    canvas.alpha_composite(img, (pad, pad))
    return canvas


def render_plop(cue: Cue, out_dir: Path) -> tuple[int, int, list[str]]:
    """Write the pop's frames (overshoot in, shrink out); returns where they go and any warnings."""
    base = _card(cue.image, round(W * cue.width_frac))
    box = (math.ceil(base.width * 1.15), math.ceil(base.height * 1.15))
    n = max(1, round((cue.end - cue.start) * FPS))
    out_dir.mkdir(parents=True)
    cache: dict[tuple[float, float], Image.Image] = {}
    for f in range(n):
        t = f / FPS
        left = (cue.end - cue.start) - t
        if t < POP_IN:
            s, fade = _ease_out_back(t / POP_IN), min(1.0, t / 0.08)
        elif left < POP_OUT:
            s, fade = 1 - _ease_in_back(1 - left / POP_OUT), 1.0
        else:
            s, fade = 1.0, 1.0
        key = (round(s, 4), round(fade, 3))
        if key not in cache:
            frame = Image.new("RGBA", box, (0, 0, 0, 0))
            if s > 0.01:
                im = base.resize((max(1, round(base.width * s)), max(1, round(base.height * s))), Image.LANCZOS)
                if fade < 1:
                    im.putalpha(im.getchannel("A").point(lambda a: int(a * fade)))
                frame.alpha_composite(im, ((box[0] - im.width) // 2, (box[1] - im.height) // 2))
            cache[key] = frame
        cache[key].save(out_dir / f"{f:05d}.png", compress_level=1)
    ## The picture itself (not its shadow) placed inside the safe zone.
    sl, st, sr, sb = SAFE
    pad = SHADOW_BLUR * 3
    iw, ih = base.width - 2 * pad, base.height - 2 * pad - SHADOW_OFFSET
    cx = sl + iw / 2 + cue.anchor[0] * max(0, (sr - sl) - iw)
    cy = st + ih / 2 + cue.anchor[1] * max(0, (sb - st) - ih)
    warnings = []
    if iw > sr - sl or ih > sb - st:
        warnings.append(f"{cue.image.name}: larger than the safe zone, reaches under the platform's controls")
    return round(cx - box[0] / 2), round(cy - box[1] / 2 + SHADOW_OFFSET / 2), warnings


def _pop_wav(path: Path) -> None:
    """A short "plop": a falling sine with a fast decay."""
    sr, dur = 48000, 0.14
    frames = bytearray()
    phase = 0.0
    for i in range(int(sr * dur)):
        t = i / sr
        phase += 2 * math.pi * (1100 * math.exp(-18 * t) + 180) / sr
        env = (1 - math.exp(-t * 900)) * math.exp(-t * 32)
        frames += struct.pack("<h", int(0.7 * env * math.sin(phase) * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))


# ---------------------------------------------------------------- the cut

def silences(video: Path) -> list[Span]:
    """Every silence of at least 0.15 s, from the level; one running to the end of the file ends there."""
    duration, _ = probe(video)
    r = subprocess.run([ffmpeg(), "-hide_banner", "-nostats", "-i", str(video), "-af", SILENCE, "-f", "null", "-"],
                       capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    return list(zip(starts, ends + [duration] * (len(starts) - len(ends))))


def speech_bounds(video: Path) -> tuple[float, float] | None:
    """First and last audible sound, from the level (Whisper's word starts run early)."""
    duration, _ = probe(video)
    r = subprocess.run([ffmpeg(), "-hide_banner", "-nostats", "-i", str(video), "-af", SILENCE, "-f", "null", "-"],
                       capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    if not starts:
        return None
    ## Silence at the very start ends at the first sound; silence at the end runs to the end of the file.
    first = ends[0] if starts[0] < 0.05 and ends else 0.0
    trailing = len(starts) > len(ends) or ends[-1] >= duration - 0.05
    return first, starts[-1] if trailing else duration


def cut_range(spec: str | None, video: Path, duration: float, *, pad_start: float, pad_end: float) -> tuple[float, float]:
    """``None`` = the whole clip; ``auto`` = from ``pad_start`` before the first sound to ``pad_end``
    after the last; ``START-END`` in the raw clip's time."""
    if spec is None:
        return 0.0, duration
    if spec == "auto":
        bounds = speech_bounds(video)
        if bounds is None:
            raise VideoError("--trim auto: no silence at the edges; give START-END instead")
        print(f"     speech {bounds[0]:.2f}–{bounds[1]:.2f} s")
        a, z = bounds[0] - pad_start, bounds[1] + pad_end
    else:
        try:
            s, e = spec.split("-")
            a, z = parse_time(s), parse_time(e)
        except ValueError as error:
            raise VideoError(f"--trim {spec!r}: expected auto or START-END") from error
    return max(0.0, a), min(duration, z)


# ---------------------------------------------------------------- loudness

def _loudness(inputs: list[str], graph: str, label: str) -> float:
    """Integrated loudness of ``label`` (loudnorm's measuring pass)."""
    probe_graph = graph + f";[{label}]loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json[m]"
    r = subprocess.run([ffmpeg(), "-hide_banner", "-nostats", *inputs, "-filter_complex", probe_graph,
                        "-map", "[m]", "-f", "null", "-"], capture_output=True, text=True, check=True)
    return float(json.loads(r.stderr[r.stderr.rindex("{"):r.stderr.rindex("}") + 1])["input_i"])


# ---------------------------------------------------------------- the render

@dataclass
class Job:
    video: Path
    out: Path
    cues: list[Cue] = field(default_factory=list)
    ## The raw clip's words; ``None`` draws no captions.
    words: list[Word] | None = None
    fit: str = "blur"
    mirror: bool = False
    trim: tuple[float, float] | None = None
    pop_sound: bool = True
    pop_volume: float = 0.35
    audio_fix: bool = True
    ## A cleaned voice on the raw clip's clock (``denoise.clean_voice``) instead of the clip's sound.
    voice: Path | None = None
    ## The kept segments (``cuts.Timeline``); overrides ``trim``.
    keep: Timeline | None = None
    layout: Layout = field(default_factory=Layout)
    ## The second clip of ``stack`` and ``pip``, and its track (empty: the whole clip fitted to the cut).
    screen: Path | None = None
    screen_spans: list[ScreenSpan] = field(default_factory=list)
    ## A headline across the top of the safe zone, for the first ``title_until`` seconds or throughout.
    title: str | None = None
    title_font: Path | None = None
    title_until: float | None = None


@dataclass(frozen=True)
class Result:
    duration: float
    t0: float
    cues: list[Cue]
    words: list[Word]
    loudness: float | None
    warnings: list[str]


def _fit(src: str, fit: str) -> str:
    """The graph that fits one clip into the whole frame: cropped, or on a blurred copy of itself."""
    if fit == "crop":
        return f"{src}scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS}[v0]"
    return (
        f"{src}fps={FPS},split[a][b];"
        f"[a]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
        f"gblur=sigma=12,eq=brightness=-0.06,scale={W}:{H}[bg];"
        f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease,setsar=1[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]"
    )


def _still_input(image: Path, length: float) -> list[str]:
    return ["-loop", "1", "-framerate", str(FPS), "-t", f"{length:.3f}", "-i", str(image)]


def _segments(job: Job, timeline: Timeline, inputs: list[str], pre: str, has_audio: bool,
              work: Path) -> tuple[list[str], list[str]]:
    """The graph for a clip cut into several segments, or laid out with a second clip: every segment
    is its own input, picture and sound are joined together so they stay in step, and the result is
    ``[v0]`` (and ``[acat]``). Also returns the graph of the sound alone, for the measuring passes."""
    layout, segs = job.layout, timeline.segments
    n, duration = len(segs), timeline.duration
    for source in [job.video] + ([job.voice] if job.voice is not None else []):
        for a, b in segs:
            inputs += ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(source)]
    sl, st, sr, sb = SAFE
    upper = 2 * round(H * layout.split / 2)
    bubble = 2 * round(W * layout.bubble / 2)
    field_size = {"full": (W, H), "stack": (W, H - upper), "pip": (bubble, bubble)}[layout.kind]
    cropped = layout.kind != "full" or job.fit == "crop"
    anchor = (0.5, 0.5) if layout.kind == "full" else layout.face_anchor
    graph, sound = [], []
    for k, (a, b) in enumerate(segs):
        zoom = 1 + layout.punch if k % 2 else 1.0
        shape = panel(*field_size, anchor, zoom) if cropped else "setsar=1"
        graph.append(f"[{k}:v]{pre}fps={FPS},{shape}[f{k}]")
        if has_audio:
            ## 15 ms fades so a cut does not click.
            sound.append(f"[{(n if job.voice is not None else 0) + k}:a]aresample=48000,afade=t=in:d=0.015,"
                         f"afade=t=out:st={b - a - 0.015:.3f}:d=0.015[a{k}]")
    graph += sound
    face = "v0" if layout.kind == "full" and cropped else "face"
    graph.append("".join(f"[f{k}]" + (f"[a{k}]" if has_audio else "") for k in range(n))
                 + f"concat=n={n}:v=1:a={int(has_audio)}[{face}]" + ("[acat]" if has_audio else ""))
    sound = sound + ["".join(f"[a{k}]" for k in range(n)) + f"concat=n={n}:v=0:a=1[acat]"]
    if layout.kind == "full":
        if not cropped:
            graph.append(_fit("[face]", "blur"))
        return graph, sound

    if job.screen is None:
        raise VideoError(f"layout {layout.kind}: needs the second clip")
    source_duration, _ = probe(job.screen)
    screen_pre = TONEMAP if transfer(job.screen) in HDR_TRANSFERS else ""
    screen_size = (W, upper) if layout.kind == "stack" else (W, H)
    shown = slots(job.screen_spans, timeline, source_duration)
    for j, slot in enumerate(shown):
        inputs += ["-ss", f"{slot.source_start:.3f}", "-t", f"{slot.source_length:.3f}", "-i", str(job.screen)]
        graph.append(f"[{inputs.count('-i') - 1}:v]{screen_pre}setpts=(PTS-STARTPTS)/{slot.speed:.5f},fps={FPS},"
                     f"{panel(*screen_size, slot.focus)},tpad=stop_mode=clone:stop_duration={slot.length:.3f},"
                     f"trim=duration={slot.length:.3f},setpts=PTS-STARTPTS,format=yuv420p[s{j}]")
        print(f"     screen {j + 1}: {slot.source_start:.2f} s for {slot.length:.2f} s"
              + (f", {slot.speed:.2f}x" if slot.speed > 1.005 else ""))
    graph.append("".join(f"[s{j}]" for j in range(len(shown))) + f"concat=n={len(shown)}:v=1:a=0[screen]")
    if layout.kind == "stack":
        graph.append("[face]format=yuv420p[facev];[screen][facev]vstack=inputs=2[v0]")
        return graph, sound
    mask, ring = work / "bubble-mask.png", work / "bubble-ring.png"
    bubble_images(bubble, mask, ring)
    inputs += _still_input(mask, duration) + _still_input(ring, duration)
    last = inputs.count("-i") - 1
    x = round(sl + layout.bubble_anchor[0] * (sr - sl - bubble))
    y = round(st + layout.bubble_anchor[1] * (sb - st - bubble))
    graph.append(f"[{last - 1}:v]format=gray[bm];[face][bm]alphamerge[fb];"
                 f"[screen][fb]overlay={x}:{y}:format=auto[pb];[pb][{last}:v]overlay={x}:{y}:format=auto[v0]")
    return graph, sound


def render(job: Job) -> Result:
    """Render ``job`` to ``job.out``; cues and words come back on the cut's clock."""
    raw_duration, has_audio = probe(job.video)
    timeline = job.keep or Timeline((job.trim or (0.0, raw_duration),))
    t0, duration = timeline.segments[0][0], timeline.duration
    cues = timeline.clip(job.cues)
    words = timeline.clip(job.words or [])
    warnings: list[str] = []
    ## One uncut clip alone in the frame goes through the graph it always did.
    simple = len(timeline.segments) == 1 and job.layout.kind == "full"
    upper = 2 * round(H * job.layout.split / 2)

    work = Path(tempfile.mkdtemp(prefix="asset-video-"))
    try:
        has_audio = has_audio or job.voice is not None
        hdr = transfer(job.video) in HDR_TRANSFERS
        if hdr:
            print("     HDR source: tone-mapped to SDR BT.709")
        if simple:
            inputs = ["-ss", f"{t0:.3f}", "-t", f"{duration:.3f}", "-i", str(job.video)]
            if job.voice is not None:
                inputs += ["-ss", f"{t0:.3f}", "-t", f"{duration:.3f}", "-i", str(job.voice)]
            src = "[0:v]" + (TONEMAP.rstrip(",") + "[sdr];[sdr]" if hdr else "")
            src = src + "hflip[src];[src]" if job.mirror else src
            graph, sound = [_fit(src, job.fit)], []
        else:
            if job.layout.punch and job.layout.kind == "full" and job.fit != "crop":
                warnings.append("punch-in needs --fit crop or a layout with fields; left out")
            inputs = []
            graph, sound = _segments(job, timeline, inputs,
                                     (TONEMAP if hdr else "") + ("hflip," if job.mirror else ""), has_audio, work)
            print(f"     {len(timeline.segments)} segments, {duration:.2f} s of {raw_duration:.2f} s")

        last = "v0"
        for i, cue in enumerate(cues):
            if cue.full:
                length = cue.end - cue.start
                inputs += (["-t", f"{length:.3f}", "-i", str(cue.image)] if cue.image.suffix.lower() in VIDEO_SUFFIXES
                           else _still_input(cue.image, length))
                graph.append(f"[{inputs.count('-i') - 1}:v]fps={FPS},{panel(W, H, (0.5, 0.5))},"
                             f"setpts=PTS-STARTPTS+{cue.start}/TB[p{i}]")
                x = y = 0
                print(f"     cutaway {i + 1}: {cue.image.name} {cue.start:.2f}–{cue.end:.2f} s")
            else:
                x, y, found = render_plop(cue, work / f"plop{i}")
                warnings += found
                inputs += ["-framerate", str(FPS), "-i", str(work / f"plop{i}" / "%05d.png")]
                graph.append(f"[{inputs.count('-i') - 1}:v]setpts=PTS-STARTPTS+{cue.start}/TB[p{i}]")
                print(f"     pop {i + 1}: {cue.image.name} {cue.start:.2f}–{cue.end:.2f} s at ({x}, {y})")
            graph.append(f"[{last}][p{i}]overlay={x}:{y}:eof_action=pass:format=auto[v{i + 1}]")
            last = f"v{i + 1}"

        if job.title:
            if job.title_font is None:
                raise VideoError("a title needs its face (the engine holds none)")
            sl, st, sr, _ = SAFE
            card = work / "title.png"
            tw, _ = title_card(job.title, job.title_font, card, width=sr - sl)
            inputs += _still_input(card, duration)
            until = f":enable='lte(t,{job.title_until:.2f})'" if job.title_until else ""
            graph.append(f"[{last}][{inputs.count('-i') - 1}:v]overlay={sl + (sr - sl - tw) // 2}:{st + 30}"
                         f":format=auto{until}[vt]")
            last = "vt"

        if job.words is not None:
            ass = work / "captions.ass"
            ## Stacked, the captions sit on the seam: over the lower edge of the second clip.
            baseline = upper + 56 if job.layout.kind == "stack" else CAPTION_BASELINE
            captions_ass(words, ass, width=W, height=H, baseline=baseline)
            esc = str(ass).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
            graph.append(f"[{last}]subtitles='{esc}'[vc]")
            last = "vc"
        graph.append(f"[{last}]setsar=1,format=yuv420p[vout]")

        audio, loudness = [], None
        if has_audio:
            chain = (CLEANED_VOICE_CHAIN if job.voice is not None else VOICE_CHAIN) if job.audio_fix else "anull"
            if simple:
                ## 15 ms fades so a cut does not click.
                fades = f",afade=t=in:d=0.015,afade=t=out:st={duration - 0.015:.3f}:d=0.015" if job.trim else ""
                source = "[1:a]" if job.voice is not None else "[0:a]"
                audio.append(f"{source}aresample=48000,{chain}{fades}[voice]")
            else:
                audio.append(f"[acat]{chain}[voice]")
            label = "voice"
            pops = [c for c in cues if c.pop_sound] if job.pop_sound else []
            if pops:
                wav = work / "pop.wav"
                _pop_wav(wav)
                inputs += ["-i", str(wav)]
                audio.append(f"[{inputs.count('-i') - 1}:a]aresample=48000,volume={job.pop_volume},asplit={len(pops)}"
                             + "".join(f"[ps{k}]" for k in range(len(pops))))
                for k, c in enumerate(pops):
                    audio.append(f"[ps{k}]adelay={round(c.start * 1000)}:all=1[pd{k}]")
                audio.append(f"[voice]{''.join(f'[pd{k}]' for k in range(len(pops)))}"
                             f"amix=inputs={len(pops) + 1}:duration=first:normalize=0[mix]")
                label = "mix"
            if job.audio_fix:
                measured = _loudness(inputs, ";".join(sound + audio), label)
                gain = TARGET_LUFS - measured
                print(f"     loudness {measured:.1f} LUFS, {gain:+.1f} dB")
                for _ in range(3):
                    loudness = _loudness(inputs, ";".join(sound + audio + [f"[{label}]volume={gain:.2f}dB,{LIMITER}[lim]"]),
                                         "lim")
                    print(f"     behind the limiter {loudness:.1f} LUFS")
                    if abs(TARGET_LUFS - loudness) < 0.3:
                        break
                    gain += TARGET_LUFS - loudness
                audio.append(f"[{label}]volume={gain:.2f}dB,{LIMITER},aresample=48000[aout]")
            else:
                audio.append(f"[{label}]anull[aout]")

        job.out.parent.mkdir(parents=True, exist_ok=True)
        print("     encoding ...", flush=True)
        subprocess.run([
            ffmpeg(), "-hide_banner", "-y", "-loglevel", "error", *inputs,
            "-filter_complex", ";".join(graph + audio),
            "-map", "[vout]", *(["-map", "[aout]"] if has_audio else []),
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-r", str(FPS),
            *(["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"] if hdr else []),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", "-t", f"{duration:.3f}", str(job.out),
        ], check=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return Result(duration, t0, cues, words, loudness, warnings)


def still(video: Path, at: float, out: Path) -> None:
    """One frame of ``video`` at ``at`` seconds, as JPEG."""
    subprocess.run([ffmpeg(), "-hide_banner", "-y", "-loglevel", "error", "-ss", f"{at:.3f}", "-i", str(video),
                    "-frames:v", "1", "-q:v", "2", str(out)], check=True)


def cover(video: Path, at: float, out: Path, *, title: str, font: Path, mirror: bool = False) -> None:
    """The frame at ``at`` of the **raw** clip (so no caption is burned in), tone-mapped and mirrored
    as the render is and cropped to the frame, with ``title`` set large in white across the top of the
    safe zone over a darkened top. Up to three lines, as large as the zone's width allows."""
    vf = (TONEMAP if transfer(video) in HDR_TRANSFERS else "") + ("hflip," if mirror else "") \
        + f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    with tempfile.TemporaryDirectory(prefix="asset-cover-") as tmp:
        frame = Path(tmp) / "frame.png"
        subprocess.run([ffmpeg(), "-hide_banner", "-y", "-loglevel", "error", "-ss", f"{at:.3f}", "-i", str(video),
                        "-vf", vf, "-frames:v", "1", str(frame)], check=True)
        img = Image.open(frame).convert("RGB")
    sl, st, sr, sb = SAFE
    ## The darkening: full at the top, gone by the middle of the frame.
    shade = Image.new("L", (1, H))
    for y in range(H):
        shade.putpixel((0, y), round(170 * max(0.0, 1 - y / (H * 0.5)) ** 1.6))
    img.paste(Image.new("RGB", (W, H), (0, 0, 0)), (0, 0), shade.resize((W, H)))

    draw = ImageDraw.Draw(img)
    face, lines, size = fit_lines(draw, title, font, sr - sl, sizes=range(150, 40, -4), max_lines=3)
    stroke, leading = max(4, size // 14), round(size * 1.12)
    y = st + 40
    for line in lines:
        draw.text((sl, y), line, font=face, fill="white", stroke_width=stroke, stroke_fill=(0, 0, 0))
        y += leading
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=92)


__all__ = ["Cue", "Job", "Result", "VideoError", "cover", "cut_range", "parse_cues", "probe", "recording_date",
           "render", "silences", "speech_bounds", "still", "transfer"]
