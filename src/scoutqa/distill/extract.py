"""Run extractor.js in a Playwright frame."""

from __future__ import annotations

from functools import cache
from importlib.resources import files

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame

from scoutqa.distill.raw import RawSnapshot

REF_ATTR = "data-scoutqa-ref"


@cache
def extractor_source() -> str:
    """The one extractor, shared with the browser extension (src/scoutqa/extension/extractor.js)."""
    return files("scoutqa").joinpath("extension", "extractor.js").read_text(encoding="utf-8")


def extension_dir() -> str:
    """Folder to load in chrome://extensions ("Load unpacked")."""
    return str(files("scoutqa").joinpath("extension"))


async def extract(frame: Frame) -> RawSnapshot | None:
    """Return the raw snapshot of one frame, or None if the frame navigated away mid-extraction."""
    try:
        await frame.evaluate(extractor_source())
        data = await frame.evaluate("() => globalThis.__scoutqaExtract()")
    except PlaywrightError:
        return None
    return RawSnapshot.model_validate(data)


def ref_selector(ref: str) -> str:
    return f'[{REF_ATTR}="{ref}"]'
