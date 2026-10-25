"""Render blocks: ```render:NAME code blocks that a renderer turns into a picture.

narrate's own extension to the script format (spec §18). The builder renders
every block before planning, then places the output exactly as it would place
`![…](file)`: same timing, fit, pan and zoom, callouts and slides on top.

Outputs are cached by a hash of the renderer, its settings, the body and the
frame, so unchanged blocks aren't rendered again. A cache hit refreshes the
file's modification time, so `narrate cache --prune` removes the least
recently used, as it does for speech.

Renderers whose output can't be reproduced later (a live web page) are
*stored* instead: in the project, under .narrate/generated/<renderer>/, with a
JSON sidecar saying what was rendered and when. Later builds reuse them, even
offline, until `narrate build --regenerate` asks for new ones.
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .config import ConfigError, parse_color
from .script import Media, Render
from .tts import cache_dir

if TYPE_CHECKING:
    from .frames import FrameRenderer


class RenderError(Exception):
    def __init__(self, message: str, line: int | None = None):
        super().__init__(message)
        self.line = line


@dataclass
class Context:
    """What a renderer may depend on, besides the block itself."""

    width: int
    height: int
    theme: str  # default | light | path to a .css file
    canvas: str | None
    frames: FrameRenderer | None = None  # the build's Chromium, for renderers that draw HTML
    project: Path | None = None  # the script's folder, where stored outputs are kept
    regenerate: bool = False  # make stored outputs again rather than reuse them


class Renderer:
    name: str
    kind = "image"  # what the output is placed as
    version = 1  # bump when the output for the same input changes, to retire cached files
    settings: frozenset[str] = frozenset()  # the frontmatter keys it takes, besides placement
    stored = False  # output can't be reproduced later: keep it in the project, not the cache
    context_keys = ("width", "height", "theme", "canvas")  # what of the Context its output depends on

    def check(self, block: Render) -> list[str]:
        """Problems with the block, found quickly, without rendering it or starting tools."""
        unknown = sorted(set(block.settings) - self.settings)
        return [f"render:{self.name} doesn't take {key!r}" for key in unknown]

    def validate(self, blocks: list[Render]) -> list[list[str]]:
        """Deeper problems, for `narrate check`: it may start tools (one go for all the blocks)."""
        return [[] for _ in blocks]

    def render(self, block: Render, ctx: Context, out: Path) -> None:
        raise NotImplementedError


RENDERERS: dict[str, Renderer] = {}


def register(cls: type[Renderer]) -> type[Renderer]:
    RENDERERS[cls.name] = cls()
    return cls


def renderer(name: str) -> Renderer:
    try:
        return RENDERERS[name]
    except KeyError:
        known = ", ".join(f"render:{n}" for n in sorted(RENDERERS))
        raise RenderError(f"unknown renderer render:{name} (known: {known})") from None


def check(blocks: list[Media]) -> list[list[str]]:
    """Every problem with each block, as `narrate check` reports them."""
    problems: list[list[str]] = []
    for media in blocks:
        try:
            problems.append(renderer(media.render.name).check(media.render))
        except RenderError as e:
            problems.append([str(e)])
    # Blocks that pass the quick checks go to their renderer's deeper one, a renderer at a time
    for name in {m.render.name for m, found in zip(blocks, problems) if not found}:
        group = [i for i, m in enumerate(blocks) if m.render.name == name and not problems[i]]
        try:
            found = renderer(name).validate([blocks[i].render for i in group])
        except RenderError as e:
            found = [[str(e)]] * len(group)
        for i, more in zip(group, found):
            problems[i] += more
    return problems


def location(block: Render, ctx: Context) -> Path:
    """Where a render block's output is kept: the project for stored renderers, else the cache."""
    r = renderer(block.name)
    key = json.dumps([r.name, r.version, block.body, block.settings, *(getattr(ctx, k) for k in r.context_keys)],
                     sort_keys=True, default=str)
    digest = hashlib.sha256(key.encode()).hexdigest()[:24]
    if r.stored and ctx.project is not None:
        return ctx.project / ".narrate" / "generated" / r.name / f"{digest}.png"
    return cache_dir() / "render" / r.name / f"{digest}.png"


def existing(block: Render, ctx: Context) -> Path | None:
    """The output a build already made for this block, if any (the studio's preview shows it)."""
    try:
        path = location(block, ctx)
    except RenderError:
        return None
    return path if path.exists() else None


def output(media: Media, ctx: Context) -> Path:
    """The rendered file for a render block, from the cache when it's there."""
    r = renderer(media.render.name)
    problems = r.check(media.render)
    if problems:
        raise RenderError("; ".join(problems), media.line)
    path = location(media.render, ctx)
    if path.exists() and not (r.stored and ctx.regenerate):
        if not r.stored:
            try:
                path.touch()
            except OSError:
                # Timestamp refresh is only a hint for pruning; ignore if we can't update it.
                pass
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f"{path.stem}.part.png")
    try:
        r.render(media.render, ctx, part)
    except RenderError as e:
        part.unlink(missing_ok=True)
        raise RenderError(str(e), e.line or media.line) from None
    part.replace(path)
    if r.stored:
        sidecar = {"renderer": r.name, "settings": media.render.settings, "body": media.render.body,
                   "frame": [ctx.width, ctx.height], "made": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        path.with_suffix(".json").write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    return path


def colour(block: Render, key: str, default: str) -> str:
    try:
        return parse_color(block.settings.get(key, default), key)
    except ConfigError as e:
        raise RenderError(f"render:{block.name}: {e}") from None


@register
class Placeholder(Renderer):
    """A card standing in for a picture still to come: the body as its label."""

    name = "placeholder"
    settings = frozenset({"background", "color"})

    def check(self, block: Render) -> list[str]:
        problems = super().check(block)
        for key in ("background", "color"):
            try:
                colour(block, key, "grey")
            except RenderError as e:
                problems.append(str(e))
        return problems

    def render(self, block: Render, ctx: Context, out: Path) -> None:
        ctx.frames.screenshot(self.page(block, ctx.width, ctx.height), out)

    def page(self, block: Render, w: int, h: int) -> str:
        """The card as an HTML page at w×h (the studio's preview shows it as it is)."""
        background, fg = colour(block, "background", "#d9d9d9"), colour(block, "color", "#4d4d4d")
        label = html.escape(block.body.strip()) or "Placeholder"
        size = round(min(w, h) * 0.07)
        page = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; width: {w}px; height: {h}px; overflow: hidden; background: {background}; }}
svg {{ position: absolute; inset: 0; }}
.label {{ position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center;
          justify-content: center; text-align: center; padding: 0 12%; box-sizing: border-box;
          color: {fg}; font: bold {size}px/1.25 "Liberation Sans", "DejaVu Sans", sans-serif; }}
.label span {{ background: {background}; padding: .2em .5em; white-space: pre-wrap; }}
.size {{ font-size: .45em; font-weight: normal; opacity: .8; margin-top: .6em; }}
</style></head><body>
<svg width="{w}" height="{h}"><g stroke="{fg}" stroke-opacity=".35" stroke-width="{max(2, w // 400)}" fill="none">
<rect x="0" y="0" width="{w}" height="{h}" stroke-width="{max(4, w // 160)}"/>
<line x1="0" y1="0" x2="{w}" y2="{h}"/><line x1="{w}" y1="0" x2="0" y2="{h}"/></g></svg>
<div class="label"><span>{label}</span><span class="size">{w} × {h}</span></div>
</body></html>"""
        return page


from . import mermaid, web  # noqa: E402,F401 -- register render:mermaid and render:web
