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
    r"^##\s*Prompt\s*(\d+)\s*(?:[|｜:：]\s*(.+?))?\s*$", flags=re.MULTILINE
)
FENCED_CODE_BLOCK_REGEX = re.compile(r"```[^\n]*\n([\s\S]*?)\n```", flags=re.MULTILINE)

ASCII_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,}")
CJK_SEQ_RE = re.compile(r"[\u4e00-\u9fff]{2,}")

CN_DIGITS: dict[str, int] = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

STOPWORDS = {
    "agent",
    "agents",
    "agentic",
    "workflow",
    "问题",
    "方法",
    "方法论",
    "核心",
    "阶段",
    "我们",
    "如何",
    "什么",
    "为什么",
    "不是",
    "一个",
    "以及",
    "一些",
    "可以",
    "需要",
    "真正",
}

SYNONYM_REPLACEMENTS: list[tuple[str, str]] = [
    ("报告", "report"),
    ("Report", "report"),
    ("report", "report"),
    ("规格", "spec"),
    ("Spec", "spec"),
    ("spec", "spec"),
    ("上下文", "context"),
    ("Context", "context"),
    ("context", "context"),
    ("命令行", "cli"),
    ("CLI", "cli"),
    ("cli", "cli"),
    ("图形界面", "gui"),
    ("界面", "gui"),
    ("GUI", "gui"),
    ("gui", "gui"),
    ("结论", "summary"),
    ("总结", "summary"),
    ("要点", "summary"),
    ("Takeaways", "summary"),
    ("takeaways", "summary"),
    ("结尾", "ending"),
    ("结束语", "ending"),
]


@dataclass(frozen=True)
class PromptInfo:
    slide: int
    title: str
    prompt: str
    body_md: str


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
        body_md = _strip_leading_trailing_md_separators(text[start:end])
        if not body_md:
            raise SystemExit(f"Empty prompt body for Prompt {slide_no} in {path}.")
        m_code = FENCED_CODE_BLOCK_REGEX.search(body_md)
        prompt_text = m_code.group(1).strip() if m_code else body_md.strip()
        if not prompt_text:
            raise SystemExit(f"Empty prompt text for Prompt {slide_no} in {path}.")
        if slide_no in prompts:
            raise SystemExit(f"Duplicate Prompt {slide_no} in {path}.")
        prompts[slide_no] = PromptInfo(slide=slide_no, title=title, prompt=prompt_text, body_md=body_md)
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
        sections.append(TalkSection(anchor="sec-00", heading="开场", body_md=intro_md, slide=None))

    for idx, start_idx in enumerate(section_starts):
        end_idx = section_starts[idx + 1] if idx + 1 < len(section_starts) else len(lines)
        heading = lines[start_idx][3:].strip()
        body_md = "\n".join(lines[start_idx + 1 : end_idx]).strip()

        section_num = _extract_section_num(heading)
        slide = None
        anchor = f"sec-{len(sections):02d}"
        sections.append(TalkSection(anchor=anchor, heading=heading, body_md=body_md, slide=slide))

    return title, sections


def _cn_numeral_to_int(text: str) -> int | None:
    s = text.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if any(ch not in CN_DIGITS and ch not in {"十"} for ch in s):
        return None
    if s == "十":
        return 10

    if "十" not in s:
        # Single digit.
        if len(s) == 1 and s in CN_DIGITS:
            return CN_DIGITS[s]
        return None

    parts = s.split("十")
    if len(parts) != 2:
        return None
    left, right = parts[0], parts[1]
    tens = 1 if left == "" else CN_DIGITS.get(left)
    if tens is None:
        return None
    ones = 0 if right == "" else CN_DIGITS.get(right)
    if ones is None:
        return None
    return tens * 10 + ones


def _extract_section_int(heading: str) -> int | None:
    section_num = _extract_section_num(heading)
    if not section_num:
        return None
    return _cn_numeral_to_int(section_num)


def _parse_move_after_spec(spec: str) -> tuple[list[int], int]:
    raw = spec.strip()
    if ":" not in raw:
        raise SystemExit(f"Invalid --move-after value: {spec!r}. Expected format like '8,9:4'.")
    left, right = raw.split(":", 1)
    move_raw = [s.strip() for s in left.split(",") if s.strip()]
    if not move_raw:
        raise SystemExit(f"Invalid --move-after value: {spec!r}. Missing move section list.")
    move_nums: list[int] = []
    for s in move_raw:
        n = _cn_numeral_to_int(s)
        if n is None:
            raise SystemExit(f"Invalid --move-after value: {spec!r}. Bad section number: {s!r}.")
        move_nums.append(n)
    after_n = _cn_numeral_to_int(right.strip())
    if after_n is None:
        raise SystemExit(f"Invalid --move-after value: {spec!r}. Bad after-section number: {right.strip()!r}.")
    return move_nums, after_n


def move_sections_after(*, sections: list[TalkSection], move_numbers: list[int], after_number: int) -> list[TalkSection]:
    move_set = set(move_numbers)
    if not move_set:
        return sections

    to_move: list[TalkSection] = []
    remaining: list[TalkSection] = []
    for sec in sections:
        n = _extract_section_int(sec.heading)
        if n is not None and n in move_set:
            to_move.append(sec)
        else:
            remaining.append(sec)

    if not to_move:
        return sections

    insert_after_idx: int | None = None
    for i, sec in enumerate(remaining):
        n = _extract_section_int(sec.heading)
        if n == after_number:
            insert_after_idx = i
            break

    if insert_after_idx is None:
        return remaining + to_move

    return remaining[: insert_after_idx + 1] + to_move + remaining[insert_after_idx + 1 :]


def _int_to_cn_numeral(n: int) -> str:
    if n <= 0:
        raise ValueError("n must be positive")
    digits = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九"}
    if n < 10:
        return digits[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + digits[n - 10]
    if n < 100:
        tens, ones = divmod(n, 10)
        out = digits[tens] + "十"
        if ones:
            out += digits[ones]
        return out
    raise ValueError("n too large for Chinese numeral conversion")


HEADING_NUMBER_PREFIX_RE = re.compile(r"^([一二三四五六七八九十〇零两0-9]+)、\s*(.*)$")


def renumber_headings(sections: list[TalkSection]) -> list[TalkSection]:
    counter = 0
    out: list[TalkSection] = []
    for sec in sections:
        m = HEADING_NUMBER_PREFIX_RE.match(sec.heading.strip())
        if not m:
            out.append(sec)
            continue
        counter += 1
        rest = m.group(2).strip()
        new_heading = f"{_int_to_cn_numeral(counter)}、{rest}"
        out.append(TalkSection(anchor=sec.anchor, heading=new_heading, body_md=sec.body_md, slide=sec.slide))
    return out


def _normalize_for_match(text: str) -> str:
    t = text
    for src, dst in SYNONYM_REPLACEMENTS:
        t = t.replace(src, f" {dst} ")
    t = t.lower()
    # Normalize punctuation to spaces.
    t = re.sub(r"[\t\r\n\u3000]+", " ", t)
    t = re.sub(r"[`~!@#$%^&*()\-_=+\[\]{}\\|;:'\",.<>/?，。、《》【】（）“”‘’：；？！…—–→←]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _extract_match_tokens(text: str) -> set[str]:
    t = _normalize_for_match(text)
    tokens: set[str] = set()

    for m in ASCII_TOKEN_RE.finditer(t):
        tok = m.group(0)
        if tok in STOPWORDS:
            continue
        tokens.add(tok)

    for m in CJK_SEQ_RE.finditer(t):
        seq = m.group(0)
        if seq in STOPWORDS:
            continue
        # Always include short seqs as-is.
        if len(seq) <= 4:
            tokens.add(seq)
            continue
        # For long CJK sequences, include n-grams up to length 4 to improve matching.
        for n in range(2, 5):
            for i in range(0, len(seq) - n + 1):
                sub = seq[i : i + n]
                if sub in STOPWORDS:
                    continue
                tokens.add(sub)

    return tokens


def _score_tokens(a: set[str], b: set[str]) -> int:
    score = 0
    for tok in a.intersection(b):
        # Favor longer matches while keeping short meaningful phrases usable.
        score += len(tok) * len(tok)
    return score


def _guess_slide_for_section(
    *,
    heading: str,
    prompts: dict[int, PromptInfo],
    exclude_slides: set[int],
    min_score: int = 8,
) -> int | None:
    heading_tokens = _extract_match_tokens(heading)
    if not heading_tokens:
        return None

    best_slide: int | None = None
    best_score = 0

    for slide_no, p in prompts.items():
        if slide_no in exclude_slides:
            continue
        prompt_tokens = _extract_match_tokens(p.title)
        score = _score_tokens(heading_tokens, prompt_tokens)
        if score > best_score:
            best_score = score
            best_slide = slide_no

    if best_slide is None or best_score < min_score:
        return None
    return best_slide


def apply_auto_slide_mapping(*, sections: list[TalkSection], prompts: dict[int, PromptInfo]) -> list[TalkSection]:
    used_slides: set[int] = set()
    assigned_by_section: dict[int, int] = {}

    # Prefer using Prompt 1 (cover) for the intro section, if present.
    for i, sec in enumerate(sections):
        if i == 0 and sec.heading == "开场" and 1 in prompts:
            assigned_by_section[i] = 1
            used_slides.add(1)
            break

    prompt_tokens_by_slide = {slide: _extract_match_tokens(p.title) for slide, p in prompts.items()}
    section_tokens_by_index = {i: _extract_match_tokens(sec.heading) for i, sec in enumerate(sections)}

    candidates: list[tuple[int, int, int]] = []  # (score, section_idx, slide_no)
    for sec_idx, sec in enumerate(sections):
        if sec_idx in assigned_by_section:
            continue
        sec_tokens = section_tokens_by_index.get(sec_idx) or set()
        if not sec_tokens:
            continue
        for slide_no, prompt_tokens in prompt_tokens_by_slide.items():
            if slide_no in used_slides:
                continue
            score = _score_tokens(sec_tokens, prompt_tokens)
            if score > 0:
                candidates.append((score, sec_idx, slide_no))

    # Greedy max-score matching (unique slide per section, unique section per slide).
    candidates.sort(key=lambda x: (-x[0], x[1], x[2]))

    min_score = 8
    for score, sec_idx, slide_no in candidates:
        if score < min_score:
            break
        if sec_idx in assigned_by_section:
            continue
        if slide_no in used_slides:
            continue
        assigned_by_section[sec_idx] = slide_no
        used_slides.add(slide_no)

    mapped: list[TalkSection] = []
    for i, sec in enumerate(sections):
        slide = assigned_by_section.get(i)
        mapped.append(TalkSection(anchor=sec.anchor, heading=sec.heading, body_md=sec.body_md, slide=slide))
    return mapped


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
        "--no-talk",
        action="store_true",
        help="Build prompts-only reading page (ignore talk.md).",
    )
    parser.add_argument(
        "--move-after",
        action="append",
        default=[],
        help="Reorder talk sections: '8,9:4' moves sections 8 and 9 to after section 4 (Arabic digits or Chinese numerals).",
    )
    parser.add_argument(
        "--embed-images",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Embed slide images as base64 for a single chosen model (default: false).",
    )
    args = parser.parse_args(argv)

    if not args.prompts.exists():
        raise SystemExit(f"Missing {args.prompts}.")
    if not args.manifest.exists():
        raise SystemExit(f"Missing {args.manifest}. Run generate_slides_with_gemini.py first.")

    prompts = parse_prompts_md(args.prompts)
    use_talk = not args.no_talk
    if use_talk:
        if not args.talk.exists():
            raise SystemExit(f"Missing {args.talk}. Pass --no-talk to build prompts-only reading page.")
        title, sections = parse_talk_md(args.talk)
        sections = apply_auto_slide_mapping(sections=sections, prompts=prompts)
        for spec in args.move_after:
            move_nums, after_n = _parse_move_after_spec(spec)
            sections = move_sections_after(sections=sections, move_numbers=move_nums, after_number=after_n)
        if args.move_after:
            sections = renumber_headings(sections)
    else:
        title = args.prompts.parent.name if args.prompts.parent.name else args.prompts.stem
        sections = [
            TalkSection(
                anchor=f"sec-{i:02d}",
                heading=f"Prompt {p.slide}：{p.title}" if p.title else f"Prompt {p.slide}",
                body_md=p.body_md,
                slide=p.slide,
            )
            for i, p in enumerate([prompts[k] for k in sorted(prompts.keys())], start=0)
        ]
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
            if use_talk and prompt and prompt.prompt:
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

    start_anchor = sections[0].anchor if sections else ""
    start_link_html = f'<a href="#{start_anchor}">开始</a>' if start_anchor else ""

    inputs = [str(args.prompts), str(args.manifest)]
    if use_talk:
        inputs.insert(0, str(args.talk))
    inputs_str = " · ".join(inputs)

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
    <script>
      // Render LaTeX math in markdown using MathJax (supports $...$ and $$...$$).
      window.MathJax = {{
        tex: {{
          inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],
          displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],
          processEscapes: true
        }},
        options: {{
          skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']
        }}
      }};
    </script>
    <script async id="MathJax-script" src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
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
          {start_link_html}
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
        <div>输入：{inputs_str}</div>
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
