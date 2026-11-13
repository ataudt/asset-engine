"""Drafts of an app icon, drawn by an image model from a description.

Layer: asset engine.
Rules:
  - This is the one step of the engine that is not a function of its inputs. It writes only into a
    fresh directory of drafts, never to a path the app commits; a draft becomes an input when the app
    copies it to the ``master`` its logo config names.
  - The engine builds no client and reads no credentials: the app hands one over through
    ``LogoFiles.google_client``, called once per run with the configured location.
  - ``google-genai`` is the ``logos`` extra and is imported here only, when a draft is asked for.
  - A failed call is a failed draft, reported loudly with what the provider said; the other drafts
    of the run still come back. Only the provider's own error family is caught.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from asset_engine.logos.config import LogoConfig, hex_color
from asset_engine.logos.prompt import draft_prompt, refine_prompt
from asset_engine.project import get_project


class LogoGenerationError(RuntimeError):
    """The model answered, but with no image in it."""


@dataclass
class Draft:
    """One call's outcome: the file written, or why there is none."""

    index: int
    path: str | None = None
    finish_reason: str | None = None
    error: str | None = None
    tokens: dict[str, int | None] = field(default_factory=dict)


def _genai():
    try:
        from google import genai
    except ModuleNotFoundError as error:
        raise LogoGenerationError(
            "drawing logo drafts needs google-genai: pip install -e '../asset-engine[logos]'"
        ) from error
    return genai


def _image_bytes(response: Any) -> tuple[bytes, str]:
    """The first image in ``response`` and the candidate's finish reason."""
    candidates = response.candidates or []
    if not candidates:
        feedback = getattr(response, "prompt_feedback", None)
        raise LogoGenerationError(f"no candidate in the response (prompt feedback: {feedback})")
    candidate = candidates[0]
    reason = str(candidate.finish_reason)
    parts = candidate.content.parts if candidate.content and candidate.content.parts else []
    for part in parts:
        if part.inline_data and part.inline_data.data:
            return part.inline_data.data, reason
    said = " ".join(part.text for part in parts if part.text) or "(nothing)"
    raise LogoGenerationError(f"no image in the response; finish reason {reason}; the model said: {said}")


def new_drafts_dir(config: LogoConfig, out: Path | None) -> Path:
    """A directory no earlier run wrote to: ``<out or default_out_dir>/drafts/<timestamp>``."""
    base = out if out is not None else config.default_out_dir
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = base / "drafts" / stamp
    suffix = 1
    while path.exists():
        suffix += 1
        path = base / "drafts" / f"{stamp}-{suffix}"
    path.mkdir(parents=True)
    return path


def generate_drafts(
    config: LogoConfig,
    description: str,
    *,
    count: int,
    out_dir: Path,
    reference: Path | None = None,
) -> list[Draft]:
    """Ask the model for ``count`` drafts and write each as ``draft-NN.png`` into ``out_dir``, beside a
    ``run.json`` that records the model, the prompt and each call's outcome."""
    genai = _genai()
    from google.genai import errors, types

    if reference is None:
        prompt = draft_prompt(description, style=config.style, key_color=config.key_color)
        contents: list[Any] = [prompt]
    else:
        prompt = refine_prompt(description, style=config.style, key_color=config.key_color)
        contents = [types.Part.from_bytes(data=reference.read_bytes(), mime_type="image/png"), prompt]

    client = get_project().logo_files().google_client(config.location)
    if not isinstance(client, genai.Client):
        raise LogoGenerationError(f"LogoFiles.google_client returned {type(client).__name__}, not a google.genai.Client")
    request = types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(aspect_ratio="1:1"),
    )

    drafts = []
    for index in range(1, count + 1):
        draft = Draft(index=index)
        try:
            response = client.models.generate_content(model=config.model, contents=contents, config=request)
            data, draft.finish_reason = _image_bytes(response)
        except (errors.APIError, LogoGenerationError) as error:
            draft.error = f"{type(error).__name__}: {error}"
        else:
            usage = response.usage_metadata
            draft.tokens = {
                "prompt": usage.prompt_token_count,
                "output": usage.candidates_token_count,
                "thoughts": usage.thoughts_token_count,
            } if usage else {}
            path = out_dir / f"draft-{index:02d}.png"
            path.write_bytes(data)
            draft.path = path.name
        drafts.append(draft)

    run = {
        "model": config.model,
        "location": config.location,
        "description": description,
        "reference": str(reference) if reference else None,
        "key_color": hex_color(config.key_color),
        "prompt": prompt,
        "drafts": [asdict(draft) for draft in drafts],
    }
    (out_dir / "run.json").write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return drafts


__all__ = ["Draft", "LogoGenerationError", "generate_drafts", "new_drafts_dir"]
