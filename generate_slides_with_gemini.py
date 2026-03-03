#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


API_KEY_ENV_CANDIDATES = [
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_GENAI_API_KEY",
    "GENAI_API_KEY",
]


DEFAULT_MODELS = [
    "gemini-3.1-flash-image-preview",
    "gemini-3-pro-image-preview",
]


AGENTS_PROMPT_REGEX = re.compile(r'^\s*“(生成一张[\s\S]*?)”\s*$', flags=re.MULTILINE)
PROMPTS_MD_SECTION_REGEX = re.compile(
    r"^##\s*Prompt\s*(\d+)(?:\s*[|｜]\s*(.+?))?\s*$", flags=re.MULTILINE
)


@dataclass(frozen=True)
class SlidePrompt:
    slide: int
    title: str
    prompt: str


def _get_api_key() -> str:
    for key in API_KEY_ENV_CANDIDATES:
        value = os.environ.get(key)
        if value:
            return value
    raise SystemExit(
        "Missing Gemini API key. Set one of: " + ", ".join(API_KEY_ENV_CANDIDATES) + "."
    )


def _normalize_model_name(model: str) -> str:
    return model if model.startswith("models/") else f"models/{model}"


def _ensure_proxy_env() -> None:
    # The user requested to use $http_proxy (also SOCKS5). Some libraries only
    # read the uppercase variants, so mirror them conservatively.
    for k in ("http_proxy", "https_proxy", "all_proxy"):
        v = os.environ.get(k)
        if v and not os.environ.get(k.upper()):
            os.environ[k.upper()] = v


def _extract_prompts_from_agents_md(text: str) -> list[SlidePrompt]:
    prompts = [p.strip() for p in AGENTS_PROMPT_REGEX.findall(text)]
    if not prompts:
        raise SystemExit(
            "No prompts found. For AGENTS.md format, ensure each prompt is wrapped in Chinese quotes and starts with “生成一张…”."
        )
    return [SlidePrompt(slide=i, title=f"Slide {i}", prompt=p) for i, p in enumerate(prompts, start=1)]


def _strip_leading_trailing_md_separators(text: str) -> str:
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    while lines and lines[0].strip() == "---":
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    while lines and lines[-1].strip() == "---":
        lines = lines[:-1]
        while lines and not lines[-1].strip():
            lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_prompts_from_prompts_md(text: str) -> list[SlidePrompt]:
    matches = list(PROMPTS_MD_SECTION_REGEX.finditer(text))
    if not matches:
        raise SystemExit(
            "No prompts found. For prompts.md format, ensure headings look like: '## Prompt 1｜封面'."
        )

    prompts: list[SlidePrompt] = []
    for idx, m in enumerate(matches):
        slide_no = int(m.group(1))
        title = (m.group(2) or f"Prompt {slide_no}").strip()

        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = _strip_leading_trailing_md_separators(text[start:end])
        if not body:
            raise SystemExit(f"Empty prompt body for Prompt {slide_no}.")

        prompts.append(SlidePrompt(slide=slide_no, title=title, prompt=body))

    slides = [p.slide for p in prompts]
    if len(set(slides)) != len(slides):
        raise SystemExit("Duplicate prompt numbers found in prompts.md headings.")

    return sorted(prompts, key=lambda p: p.slide)


def _extract_prompts(*, text: str, source_path: Path) -> tuple[str, list[SlidePrompt]]:
    # Auto-detect file format:
    # - prompts.md: sections headed by "## Prompt N"
    # - AGENTS.md: Chinese-quoted prompts starting with “生成一张…”
    if PROMPTS_MD_SECTION_REGEX.search(text):
        return "prompts_md", _extract_prompts_from_prompts_md(text)
    return "agents_md", _extract_prompts_from_agents_md(text)


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_bytes(data)
    tmp_path.replace(path)


def _extract_first_image_part(response):
    candidates = getattr(response, "candidates", None) or []
    for cand in candidates:
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", None) if content else None
        if not parts:
            continue
        for part in parts:
            inline = getattr(part, "inline_data", None)
            if inline and getattr(inline, "data", None):
                return inline
    return None


def _generate_image_with_retry(*, client, model: str, prompt: str, config, attempts: int, base_sleep_s: float):
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = client.models.generate_content(model=model, contents=prompt, config=config)
            inline = _extract_first_image_part(resp)
            if inline is None:
                raise RuntimeError("No image returned in response")
            return resp, inline
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= attempts:
                break
            sleep_s = base_sleep_s * (2 ** (attempt - 1))
            print(
                f"[retry] generate_content failed (attempt {attempt}/{attempts}): {type(exc).__name__}: {exc}. sleeping {sleep_s:.1f}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(sleep_s)
    assert last_exc is not None
    raise last_exc


def _mime_to_ext(mime: Optional[str]) -> str:
    if not mime:
        return ".bin"
    mime = mime.lower()
    if mime == "image/png":
        return ".png"
    if mime in ("image/jpeg", "image/jpg"):
        return ".jpg"
    if mime == "image/webp":
        return ".webp"
    return ".bin"


def _safe_stem(text: str) -> str:
    # Keep it filesystem-friendly.
    t = text.strip()
    t = t.replace("models/", "")
    t = re.sub(r"[^a-zA-Z0-9._-]+", "_", t)
    t = re.sub(r"_+", "_", t).strip("_")
    return t or "model"


@dataclass(frozen=True)
class GenConfig:
    model: str
    temperature: Optional[float]
    seed: Optional[int]
    attempts: int
    base_sleep_s: float
    sleep_s: float


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Generate PPT-like slide images from prompts.md (or AGENTS.md) using Gemini image preview models."
    )
    parser.add_argument(
        "--prompts",
        "--agents",
        dest="prompts_path",
        type=Path,
        default=None,
        help="Path to prompts markdown (default: prompts.md if present, else AGENTS.md).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("images"),
        help="Output directory (default: images).",
    )
    parser.add_argument(
        "--models",
        nargs="*",
        default=DEFAULT_MODELS,
        help="Models to use (default: gemini-3.1-flash-image-preview gemini-3-pro-image-preview).",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Sampling temperature (default: model default).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed if supported (default: unset).",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=4,
        help="Retry attempts per image (default: 4).",
    )
    parser.add_argument(
        "--base-sleep-s",
        type=float,
        default=1.5,
        help="Base backoff sleep seconds for retries (default: 1.5).",
    )
    parser.add_argument(
        "--sleep-s",
        type=float,
        default=0.25,
        help="Sleep seconds between successful calls (default: 0.25).",
    )
    parser.add_argument(
        "--overwrite",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Overwrite existing images (default: true).",
    )
    args = parser.parse_args(argv)

    prompts_path: Path
    if args.prompts_path is not None:
        prompts_path = args.prompts_path
    else:
        prompts_path = Path("prompts.md") if Path("prompts.md").exists() else Path("AGENTS.md")

    _ensure_proxy_env()
    api_key = _get_api_key()

    if not prompts_path.exists():
        raise SystemExit(f"Missing {prompts_path}. Run from repo root or pass --prompts/--agents.")

    prompts_md = prompts_path.read_text(encoding="utf-8")
    prompt_format, prompts = _extract_prompts(text=prompts_md, source_path=prompts_path)

    from google import genai  # type: ignore
    from google.genai import types  # type: ignore

    # Prefer the official/simple path: GEMINI_API_KEY in env (matches docs/examples).
    client = genai.Client() if os.environ.get("GEMINI_API_KEY") else genai.Client(api_key=api_key)

    args.out_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "prompts_path": str(prompts_path),
        "prompt_format": prompt_format,
        "prompt_count": len(prompts),
        "models": [],
        "slides": [],
    }

    # Prebuild config; some fields may be unset.
    base_config_kwargs: dict[str, object] = {
        "response_modalities": ["IMAGE"],
    }
    if args.temperature is not None:
        base_config_kwargs["temperature"] = args.temperature
    if args.seed is not None:
        base_config_kwargs["seed"] = args.seed

    for model_in in args.models:
        model = _normalize_model_name(model_in)
        cfg = GenConfig(
            model=model,
            temperature=args.temperature,
            seed=args.seed,
            attempts=args.attempts,
            base_sleep_s=args.base_sleep_s,
            sleep_s=args.sleep_s,
        )

        model_dir = args.out_dir / _safe_stem(model)
        model_dir.mkdir(parents=True, exist_ok=True)
        print(f"[model] {model} -> {model_dir}", flush=True)
        (manifest["models"]).append({"name": model, "dir": str(model_dir)})

        for i, prompt in enumerate(prompts, start=1):
            out_stub = f"slide_{prompt.slide:02d}"

            # Skip if already present (any supported image ext).
            existing = None
            for ext in (".png", ".jpg", ".jpeg", ".webp", ".bin"):
                p = model_dir / f"{out_stub}{ext}"
                if p.exists():
                    existing = p
                    break
            if existing and not args.overwrite:
                print(f"[skip] {model} {out_stub} (exists: {existing})", flush=True)
                continue

            gen_config = types.GenerateContentConfig(**base_config_kwargs)
            print(f"[gen] {model} {out_stub} ...", flush=True)
            _, inline = _generate_image_with_retry(
                client=client,
                model=cfg.model,
                prompt=prompt.prompt,
                config=gen_config,
                attempts=cfg.attempts,
                base_sleep_s=cfg.base_sleep_s,
            )

            mime = getattr(inline, "mime_type", None)
            data = getattr(inline, "data", None)
            if not data:
                raise RuntimeError("Image data missing")

            ext = _mime_to_ext(mime)
            out_path = model_dir / f"{out_stub}{ext}"
            _write_bytes_atomic(out_path, data)

            # Save per-slide prompt for reproducibility.
            prompt_path = model_dir / f"{out_stub}.prompt.txt"
            prompt_path.write_text(prompt.prompt.strip() + "\n", encoding="utf-8")

            (manifest["slides"]).append(
                {
                    "model": model,
                    "slide": prompt.slide,
                    "image_path": str(out_path),
                    "prompt_path": str(prompt_path),
                    "prompt_title": prompt.title,
                    "mime_type": mime,
                }
            )

            print(f"[ok] wrote {out_path}", flush=True)
            if cfg.sleep_s > 0:
                time.sleep(cfg.sleep_s)

    manifest_path = args.out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[done] {manifest_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
