#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import markdown


PROMPTS_MD_SECTION_REGEX = re.compile(
    r"^##\s*Prompt\s*(\d+)(?:\s*[|｜]\s*(.+?))?\s*$", flags=re.MULTILINE
)

SECTION_NUM_TO_SLIDE: dict[str, int] = {
    "一": 2,
    "二": 3,
    "三": 4,
    "四": 5,
    "五": 6,
    "六": 7,
    "七": 8,
    "九": 9,
    "十二": 10,
    "十四": 11,
}


@dataclass(frozen=True)
class PromptInfo:
    slide: int
    title: str
    prompt: str


@dataclass(frozen=True)
class TalkSection:
    anchor: str
    heading: str
    body_md: str
    slide: int | None


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


def parse_prompts_md(path: Path) -> dict[int, PromptInfo]:
    text = path.read_text(encoding="utf-8")
    matches = list(PROMPTS_MD_SECTION_REGEX.finditer(text))
    if not matches:
        raise SystemExit(f"No prompts found in {path}. Expected headings like: '## Prompt 1｜封面'.")

    prompts: dict[int, PromptInfo] = {}
    for idx, m in enumerate(matches):
        slide_no = int(m.group(1))
        title = (m.group(2) or f"Prompt {slide_no}").strip()
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = _strip_leading_trailing_md_separators(text[start:end])
        if not body:
            raise SystemExit(f"Empty prompt body for Prompt {slide_no} in {path}.")
        if slide_no in prompts:
            raise SystemExit(f"Duplicate Prompt {slide_no} in {path}.")
        prompts[slide_no] = PromptInfo(slide=slide_no, title=title, prompt=body)
    return prompts


def _extract_section_num(heading: str) -> str | None:
    m = re.match(r"^([一二三四五六七八九十]+)、", heading.strip())
    if not m:
        return None
    return m.group(1)


def parse_talk_md(path: Path) -> tuple[str, list[TalkSection]]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    title = "Talk"
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        lines = lines[1:]

    # Split by top-level sections (## ...).
    section_starts: list[int] = [i for i, ln in enumerate(lines) if ln.startswith("## ")]

    sections: list[TalkSection] = []

    intro_end = section_starts[0] if section_starts else len(lines)
    intro_md = "\n".join(lines[:intro_end]).strip()
    if intro_md:
        sections.append(TalkSection(anchor="sec-00", heading="开场", body_md=intro_md, slide=1))

    for idx, start_idx in enumerate(section_starts):
        end_idx = section_starts[idx + 1] if idx + 1 < len(section_starts) else len(lines)
        heading = lines[start_idx][3:].strip()
        body_md = "\n".join(lines[start_idx + 1 : end_idx]).strip()

        section_num = _extract_section_num(heading)
        slide = SECTION_NUM_TO_SLIDE.get(section_num) if section_num else None
        anchor = f"sec-{len(sections):02d}"
        sections.append(TalkSection(anchor=anchor, heading=heading, body_md=body_md, slide=slide))

    return title, sections


def render_md(md_text: str) -> str:
    return markdown.markdown(
        md_text,
        extensions=["fenced_code", "tables", "sane_lists"],
        output_format="html5",
    )


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_image_map(manifest: dict, *, out_dir: Path, embed_images: bool, selected_model: str | None) -> tuple[list[str], str, dict]:
    slides_by_model: dict[str, dict[str, dict]] = {}

    for s in manifest.get("slides", []):
        model = str(s.get("model") or "")
        slide = str(s.get("slide"))
        image_path = Path(str(s.get("image_path") or ""))
        prompt_title = str(s.get("prompt_title") or "")
        mime = str(s.get("mime_type") or "")

        rel_src = os.path.relpath(image_path, start=out_dir)
        slides_by_model.setdefault(model, {})[slide] = {
            "src": rel_src,
            "mime": mime,
            "prompt_title": prompt_title,
            "abs_path": str(image_path),
        }

    models = [str(m.get("name")) for m in manifest.get("models", []) if m.get("name")]
    if not models:
        models = sorted(slides_by_model.keys())
    if not models:
        raise SystemExit("No slides found in manifest.")

    default_model = selected_model or models[0]
    if default_model not in slides_by_model:
        raise SystemExit(
            f"Requested model not found in manifest: {default_model}. Available: {', '.join(slides_by_model.keys())}"
        )

    if not embed_images:
        # Drop abs paths from output map.
        out_map = {
            model: {slide: {k: v for k, v in info.items() if k != "abs_path"} for slide, info in slides.items()}
            for model, slides in slides_by_model.items()
        }
        return models, default_model, out_map

    # Embed images for selected model only (avoid huge HTML).
    embedded: dict[str, dict[str, dict]] = {default_model: {}}
    for slide, info in slides_by_model[default_model].items():
        abs_path = Path(info["abs_path"])
        data = abs_path.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        mime = info.get("mime") or "application/octet-stream"
        embedded[default_model][slide] = {
            "src": f"data:{mime};base64,{b64}",
            "mime": mime,
            "prompt_title": info.get("prompt_title", ""),
        }
    return [default_model], default_model, embedded


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build a scrollable HTML reading experience (slides + talk notes).")
    parser.add_argument("--talk", type=Path, default=Path("talk.md"), help="Path to talk.md (default: talk.md).")
    parser.add_argument(
        "--prompts", type=Path, default=Path("prompts.md"), help="Path to prompts.md (default: prompts.md)."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("images/manifest.json"),
        help="Path to images/manifest.json (default: images/manifest.json).",
    )
    parser.add_argument("--out", type=Path, default=Path("reading.html"), help="Output HTML path (default: reading.html).")
    parser.add_argument("--model", type=str, default=None, help="Model name to use by default (must exist in manifest).")
    parser.add_argument(
        "--embed-images",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Embed slide images as base64 for a single chosen model (default: false).",
    )
    args = parser.parse_args(argv)

    if not args.talk.exists():
        raise SystemExit(f"Missing {args.talk}.")
    if not args.prompts.exists():
        raise SystemExit(f"Missing {args.prompts}.")
    if not args.manifest.exists():
        raise SystemExit(f"Missing {args.manifest}. Run generate_slides_with_gemini.py first.")

    prompts = parse_prompts_md(args.prompts)
    title, sections = parse_talk_md(args.talk)
    manifest = load_manifest(args.manifest)

    models, default_model, image_map = build_image_map(
        manifest,
        out_dir=args.out.parent,
        embed_images=args.embed_images,
        selected_model=args.model,
    )

    generated_at = str(manifest.get("generated_at") or "")

    toc_items = "\n".join(
        f'<li><a href="#{sec.anchor}">{sec.heading}</a></li>' for sec in sections
    )

    section_html_parts: list[str] = []
    for sec in sections:
        body_html = render_md(sec.body_md) if sec.body_md.strip() else ""

        slide_block = ""
        if sec.slide is not None:
            prompt = prompts.get(sec.slide)
            caption = f"Slide {sec.slide:02d}" + (f"｜{prompt.title}" if prompt and prompt.title else "")
            prompt_details = ""
            if prompt and prompt.prompt:
                prompt_details = (
                    "<details class=\"prompt\"><summary>Prompt</summary>"
                    f"<pre>{prompt.prompt}</pre></details>"
                )

            slide_block = (
                "<figure class=\"slide\">"
                f"<a class=\"slide-link\" data-slide-link=\"{sec.slide}\" href=\"#\">"
                f"<img class=\"slide-img\" data-slide=\"{sec.slide}\" alt=\"{caption}\" loading=\"lazy\" />"
                "</a>"
                f"<figcaption>{caption}</figcaption>"
                f"{prompt_details}"
                "</figure>"
            )

        article = f"<article class=\"notes\">{slide_block}{body_html}</article>"

        section_html_parts.append(
            f"<section class=\"section\" id=\"{sec.anchor}\">"
            f"<h2>{sec.heading}</h2>"
            f"{article}"
            "</section>"
        )

    sections_html = "\n".join(section_html_parts)

    model_options = "\n".join(
        f"<option value=\"{m}\"{' selected' if m == default_model else ''}>{m}</option>" for m in models
    )
    model_selector_html = (
        "<label class=\"control\">模型："
        f"<select id=\"modelSelect\">{model_options}</select>"
        "</label>"
        if len(models) > 1 and not args.embed_images
        else ""
    )

    html = f"""<!doctype html>
<html lang="zh-Hans">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title}</title>
    <style>
      :root {{
        --bg: #0b0d12;
        --fg: #e9eef7;
        --muted: #a9b4c6;
        --card: #111522;
        --border: rgba(255,255,255,0.10);
        --accent: #2f6bff;
        --code: #0f172a;
      }}
      @media (prefers-color-scheme: light) {{
        :root {{
          --bg: #ffffff;
          --fg: #0b1220;
          --muted: #5b667a;
          --card: #f7f8fb;
          --border: rgba(11,18,32,0.12);
          --accent: #2f6bff;
          --code: #0b1220;
        }}
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji",
          "Segoe UI Emoji";
        line-height: 1.6;
        background: var(--bg);
        color: var(--fg);
      }}
      a {{ color: var(--accent); text-decoration: none; }}
      a:hover {{ text-decoration: underline; }}
      header {{
        position: sticky;
        top: 0;
        z-index: 5;
        backdrop-filter: blur(10px);
        background: color-mix(in srgb, var(--bg) 88%, transparent);
        border-bottom: 1px solid var(--border);
      }}
      .container {{
        max-width: 1080px;
        margin: 0 auto;
        padding: 16px;
      }}
      .topbar {{
        display: flex;
        gap: 16px;
        align-items: baseline;
        justify-content: space-between;
        flex-wrap: wrap;
      }}
      h1 {{
        font-size: 20px;
        margin: 0;
      }}
      .meta {{
        color: var(--muted);
        font-size: 12px;
        margin-top: 2px;
      }}
      .controls {{
        display: flex;
        gap: 10px;
        align-items: center;
        flex-wrap: wrap;
        font-size: 12px;
        color: var(--muted);
      }}
      .control select {{
        margin-left: 6px;
        background: var(--card);
        border: 1px solid var(--border);
        color: var(--fg);
        border-radius: 8px;
        padding: 6px 10px;
      }}
      nav.toc {{
        margin: 18px 0 8px;
        padding: 14px 14px 10px;
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 14px;
      }}
      nav.toc h2 {{
        font-size: 14px;
        margin: 0 0 6px;
        color: var(--muted);
      }}
      nav.toc ol {{
        margin: 0;
        padding-left: 18px;
      }}
      nav.toc li {{
        margin: 4px 0;
      }}
      .section {{
        margin: 18px 0;
        padding: 16px;
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 18px;
      }}
      .section h2 {{
        margin: 0 0 12px;
        font-size: 16px;
      }}
      figure.slide {{
        margin: 0;
        padding: 12px;
        background: color-mix(in srgb, var(--bg) 92%, transparent);
        border: 1px solid var(--border);
        border-radius: 14px;
        float: right;
        width: min(588px, 62%);
        margin: 0 0 12px 16px;
      }}
      figure.slide img {{
        width: 100%;
        height: auto;
        display: block;
        border-radius: 10px;
        border: 1px solid var(--border);
        background: color-mix(in srgb, var(--bg) 86%, transparent);
      }}
      figure.slide figcaption {{
        margin-top: 10px;
        font-size: 12px;
        color: var(--muted);
      }}
      details.prompt {{
        margin-top: 10px;
      }}
      details.prompt summary {{
        cursor: pointer;
        color: var(--muted);
        font-size: 12px;
      }}
      details.prompt pre {{
        margin: 10px 0 0;
        padding: 12px;
        border-radius: 12px;
        overflow: auto;
        border: 1px solid var(--border);
        background: color-mix(in srgb, var(--bg) 92%, transparent);
        color: var(--fg);
        font-size: 12px;
        line-height: 1.55;
        white-space: pre-wrap;
      }}
      article.notes {{
        padding: 4px 2px;
      }}
      article.notes::after {{
        content: "";
        display: block;
        clear: both;
      }}
      article.notes p {{
        margin: 0 0 12px;
      }}
      article.notes hr {{
        border: none;
        border-top: 1px solid var(--border);
        margin: 16px 0;
      }}
      article.notes code {{
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
        font-size: 0.92em;
        padding: 0.1em 0.35em;
        border-radius: 8px;
        background: color-mix(in srgb, var(--bg) 86%, transparent);
        border: 1px solid var(--border);
      }}
      article.notes pre code {{
        display: block;
        padding: 12px;
        overflow: auto;
        border-radius: 12px;
        background: color-mix(in srgb, var(--bg) 92%, transparent);
      }}
      article.notes h3 {{
        margin: 16px 0 10px;
        font-size: 14px;
        color: var(--fg);
      }}
      @media (max-width: 920px) {{
        figure.slide {{
          float: none;
          width: 100%;
          margin: 0 0 12px 0;
        }}
      }}
      footer {{
        color: var(--muted);
        font-size: 12px;
        padding: 18px 0 30px;
      }}
    </style>
  </head>
  <body>
    <header>
      <div class="container topbar">
        <div>
          <h1>{title}</h1>
          <div class="meta">generated_at: {generated_at}</div>
        </div>
        <div class="controls">
          {model_selector_html}
          <a href="#sec-00">开场</a>
        </div>
      </div>
    </header>

    <main class="container">
      <nav class="toc">
        <h2>目录</h2>
        <ol>
          {toc_items}
        </ol>
      </nav>

      {sections_html}

      <footer>
        <div>输入：{args.talk} · {args.prompts} · {args.manifest}</div>
        <div>提示：点击 slide 图片可在新标签页打开原图</div>
      </footer>
    </main>

    <script>
      const IMAGE_MAP = {json.dumps(image_map, ensure_ascii=False)};
      const DEFAULT_MODEL = {json.dumps(default_model, ensure_ascii=False)};

      function setModel(modelName) {{
        document.querySelectorAll('img[data-slide]').forEach((img) => {{
          const slide = img.dataset.slide;
          const info = IMAGE_MAP?.[modelName]?.[slide];
          if (!info) return;
          img.src = info.src;
        }});
        document.querySelectorAll('a[data-slide-link]').forEach((a) => {{
          const slide = a.dataset.slideLink;
          const info = IMAGE_MAP?.[modelName]?.[slide];
          if (!info) return;
          a.href = info.src;
          a.target = '_blank';
          a.rel = 'noopener';
        }});
      }}

      const select = document.getElementById('modelSelect');
      if (select) {{
        select.addEventListener('change', (e) => setModel(e.target.value));
      }}
      setModel(select ? select.value : DEFAULT_MODEL);
    </script>
  </body>
</html>
"""

    args.out.write_text(html, encoding="utf-8")
    print(f"[ok] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
