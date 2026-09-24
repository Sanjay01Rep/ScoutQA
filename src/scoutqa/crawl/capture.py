"""Capture the current UI state of a page: extract every in-scope frame and distill it."""

from __future__ import annotations

from urllib.parse import urlsplit

from playwright.async_api import Page

from scoutqa.config.models import ProjectConfig
from scoutqa.crawl.results import FrameRef
from scoutqa.crawl.scope import host_allowed, normalize_url
from scoutqa.distill.build import Distilled, distill
from scoutqa.distill.extract import extract
from scoutqa.distill.raw import RawSnapshot


async def capture(page: Page, cfg: ProjectConfig) -> tuple[Distilled, list[FrameRef]] | None:
    main = await extract(page.main_frame)
    if main is None:
        return None
    frames: list[tuple[str, RawSnapshot]] = []
    refs: list[FrameRef] = []
    external: list[str] = []
    for frame in page.frames:
        if frame is page.main_frame or not frame.url.startswith("http"):
            continue
        host = urlsplit(frame.url).hostname or ""
        in_scope = host_allowed(host, cfg.allowed_domains)
        refs.append(FrameRef(url=normalize_url(frame.url) or frame.url, in_scope=in_scope))
        if not in_scope:
            external.append(f"{host} (third-party, not analysed)")
            continue
        raw = await extract(frame)
        if raw is not None:
            frames.append((frame.url, raw))
    distilled = distill(main, frames, tuple(cfg.safety.extra_unsafe_keywords), external_frames=external)
    return distilled, refs
