"""The voice separated from wind and other changing noise, once per clip, by a speech model.

Layer: asset engine.
Rules:
  - It runs once per clip: the cleaned voice goes to a cache file (48 kHz mono WAV on the raw clip's
    clock), and every later render, the transcription and the cut read that file, so a render is a
    function of that file and not of the model, as with the transcript cache.
  - The model is DeepFilterNet's standalone ``deep-filter`` binary (MIT/Apache, no torch). It comes
    from ``PATH`` or from the virtualenv's ``bin/`` beside the interpreter; the model archive
    (DeepFilterNet3) from ``--denoise-model``, else ``share/deep-filter/`` in the virtualenv, else
    the binary's own default. ``deep-filter`` reads only 48 kHz WAV.
  - ``afftdn`` (the voice chain's steady-noise stage) cannot do this: it learns one noise profile, and
    wind changes from gust to gust.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from asset_engine.video.render import VideoError, ffmpeg

INSTALL_HINT = ("download deep-filter-0.5.6-<arch>-apple-darwin from github.com/Rikorose/DeepFilterNet/releases "
                "to <venv>/bin/deep-filter (chmod +x), and models/DeepFilterNet3_onnx.tar.gz from the same repo "
                "to <venv>/share/deep-filter/")
## How far the noise is pushed down. Unlimited (100) leaves dead air that sounds cut out between words;
## at 40 dB even a gust is inaudible under the voice.
ATTEN_LIM_DB = 40


@lru_cache(maxsize=1)
def deep_filter() -> str:
    found = shutil.which("deep-filter")
    if found:
        return found
    beside = Path(sys.executable).parent / "deep-filter"
    if beside.is_file():
        return str(beside)
    raise VideoError(f"--denoise needs the deep-filter binary: {INSTALL_HINT}")


def default_model() -> Path | None:
    """DeepFilterNet3 in the virtualenv's ``share/deep-filter/``, if it was put there."""
    found = sorted((Path(sys.prefix) / "share" / "deep-filter").glob("DeepFilterNet3*.tar.gz"))
    return found[-1] if found else None


def clean_voice(video: Path, *, cache: Path, model: Path | None = None) -> Path:
    """The voice of ``video`` without the noise; read from ``cache`` when it exists, else written there."""
    if cache.is_file():
        return cache
    model = model or default_model()
    print(f"     cleaning the voice with deep-filter{f' ({model.name})' if model else ''} (once; cached afterwards) ...",
          flush=True)
    work = Path(tempfile.mkdtemp(prefix="asset-denoise-"))
    try:
        raw = work / "voice.wav"
        subprocess.run([ffmpeg(), "-hide_banner", "-y", "-loglevel", "error", "-i", str(video),
                        "-vn", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(raw)], check=True)
        out_dir = work / "out"
        ## -D: compensate the model's lookahead, or the voice runs late against the lips.
        subprocess.run([deep_filter(), *(["-m", str(model)] if model else []), "-D", "-a", str(ATTEN_LIM_DB),
                        "-o", str(out_dir), str(raw)], check=True, capture_output=True, text=True)
        cleaned = out_dir / raw.name
        if not cleaned.is_file():
            raise VideoError(f"deep-filter wrote no {cleaned.name} into {out_dir}")
        cache.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(cleaned), cache)
    except subprocess.CalledProcessError as error:
        raise VideoError(f"deep-filter failed: {(error.stderr or '')[-400:]}") from error
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return cache


__all__ = ["clean_voice", "deep_filter", "default_model"]
