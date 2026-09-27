"""A glowing name: the service's name as shimmering text on a transparent background, cut into a row of
animated custom emoji that the bot uploads as its own emoji pack.

Telegram's video emoji: WEBM with VP9 (alpha allowed), exactly 100×100, at most 3 seconds and 30 fps, at most
64 KB. The whole name is drawn on one strip ``segments × 100`` pixels wide, a colour gradient slides across
it in a seamless 2-second loop, and every 100-pixel square becomes one emoji, so in the channel they read as
one word. The drawing and encoding (Pillow, PyAV with libvpx) take a second or two and run in a thread.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import secrets
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain.fonts import Glyph

log = logging.getLogger(__name__)

TILE = 100
FPS = 30
SECONDS = 2.0
MAX_BYTES = 64 * 1024  # Telegram's limit for one video emoji
MIN_SEGMENTS, MAX_SEGMENTS = 2, 8
TEXT_SIZE = 64  # letters are drawn this high when the name fits; a longer name gets smaller letters
CRF_STEPS = (24, 30, 36, 42, 50)  # better quality first; a larger number shrinks the file
ALT = "✨"  # the plain emoji a custom emoji stands for
FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
)

# colour stops of the gradient sliding across the letters
PALETTES: dict[str, tuple[tuple[int, int, int], ...]] = {
    "gold": ((255, 196, 0), (255, 255, 255), (255, 120, 40), (255, 214, 70)),
    "neon": ((0, 229, 255), (255, 255, 255), (124, 77, 255), (0, 176, 255)),
    "pink": ((255, 64, 129), (255, 255, 255), (255, 128, 171), (234, 128, 252)),
    "rainbow": ((255, 82, 82), (255, 215, 64), (105, 240, 174), (64, 196, 255), (224, 64, 251)),
}
PALETTE_TITLES = {"gold": "🌟 Золото", "neon": "💎 Неон", "pink": "🌸 Розовый", "rainbow": "🌈 Радуга"}
DEFAULT_PALETTE = "gold"


class GlowError(Exception):
    pass


def font_path() -> str | None:
    return next((path for path in FONT_PATHS if Path(path).exists()), None)


def available() -> bool:
    """Pillow, PyAV with a VP9 encoder and a font are all there."""
    try:
        import av
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return font_path() is not None and "libvpx-vp9" in av.codecs_available


@dataclass
class Layout:
    segments: int
    size: int  # font size in pixels


def drawable(name: str) -> str:
    """The part of the name the font can draw: colour emoji and characters it lacks are left out (they would
    come out as empty boxes); spaces are collapsed."""
    from PIL import ImageFont

    path = font_path()
    if path is None:
        raise GlowError("no font")
    font = ImageFont.truetype(path, 32)
    missing = bytes(font.getmask("\U0010fffd"))  # a private-use character: the font's "no glyph" box
    kept = []
    for char in name:
        if char.isspace():
            kept.append(" ")
        elif unicodedata.category(char)[0] not in "CZ" and bytes(font.getmask(char)) != missing:
            kept.append(char)
    return " ".join("".join(kept).split())


MARGIN = 12  # pixels kept clear at each end of the row


def layout(name: str) -> Layout:
    """How many squares the name needs and at what size: short names take fewer squares. The text runs on
    across the seams: Telegram draws consecutive emoji almost touching (a pixel apart at text size), which
    reads as one word, while moving letters away from the seams would scatter them."""
    from PIL import ImageFont

    path = font_path()
    if path is None:
        raise GlowError("no font")
    if not name.strip():
        raise GlowError("nothing to draw")
    size = TEXT_SIZE
    while True:
        width = _text_width(ImageFont.truetype(path, size), name)
        segments = max(MIN_SEGMENTS, math.ceil((width + 2 * MARGIN) / TILE))
        if segments <= MAX_SEGMENTS or size <= 24:
            return Layout(min(segments, MAX_SEGMENTS), size)
        size -= 2


def _text_width(font: Any, text: str) -> int:
    box = font.getbbox(text)
    return int(box[2] - box[0])


def _mask(name: str, spec: Layout) -> Any:
    """The letters (white on black) across the whole row, centred."""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(font_path() or "", spec.size)
    width = spec.segments * TILE
    while _text_width(font, name) > width - 2 * MARGIN and font.size > 12:  # still too long: smaller
        font = ImageFont.truetype(font_path() or "", font.size - 2)
    box = font.getbbox(name)
    mask = Image.new("L", (width, TILE), 0)
    ImageDraw.Draw(mask).text(
        ((width - (box[2] - box[0])) / 2 - box[0], (TILE - (box[3] - box[1])) / 2 - box[1]),
        name,
        font=font,
        fill=255,
    )
    return mask


def _gradient(width: int, phase: float, palette: tuple[tuple[int, int, int], ...]) -> Any:
    """Colours of the strip at ``phase`` (0..1): a diagonal band of the palette, seamless over the loop."""
    import numpy as np

    x = np.arange(width, dtype=np.float32)[None, :] + np.arange(TILE, dtype=np.float32)[:, None] * 0.6
    t = (x / (width * 0.9) - phase) % 1.0
    stops = np.array((*palette, palette[0]), dtype=np.float32)
    pos = t * (len(stops) - 1)
    index = np.floor(pos).astype(int)
    frac = (pos - index)[..., None]
    return stops[index] * (1 - frac) + stops[np.minimum(index + 1, len(stops) - 1)] * frac


def frames(name: str, palette: str = DEFAULT_PALETTE, spec: Layout | None = None) -> tuple[list[Any], Layout]:
    """RGBA frames (numpy arrays) of the whole strip for one loop."""
    import numpy as np
    from PIL import ImageFilter

    spec = spec or layout(name)
    colours = PALETTES.get(palette, PALETTES[DEFAULT_PALETTE])
    mask = _mask(name, spec)
    letters = np.asarray(mask, dtype=np.float32) / 255
    halo = np.asarray(mask.filter(ImageFilter.GaussianBlur(4)), dtype=np.float32) / 255
    alpha = np.clip(letters + halo * 0.55, 0, 1)
    shade = letters[..., None] + (1 - letters[..., None]) * 0.85  # the halo a little darker than the letters
    count = int(FPS * SECONDS)
    out = []
    for k in range(count):
        rgb = _gradient(mask.width, k / count, colours) * shade
        # transparent pixels keep the gradient colour too: no dark fringe where the encoder blurs the alpha
        out.append(np.dstack([np.clip(rgb, 0, 255), alpha * 255]).astype(np.uint8))
    return out, spec


def encode(tile_frames: list[Any], crf: int) -> bytes:
    """One 100×100 VP9 WEBM with alpha."""
    import av

    buffer = io.BytesIO()
    container = av.open(buffer, mode="w", format="webm")
    stream = container.add_stream("libvpx-vp9", rate=FPS)
    stream.width = stream.height = TILE
    stream.pix_fmt = "yuva420p"
    # constant quality; alpha needs auto-alt-ref off
    stream.options = {"crf": str(crf), "b": "0", "auto-alt-ref": "0", "deadline": "good", "cpu-used": "2"}
    for array in tile_frames:
        frame = av.VideoFrame.from_ndarray(array, format="rgba").reformat(format="yuva420p")
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return buffer.getvalue()


def render(name: str, palette: str = DEFAULT_PALETTE) -> list[bytes]:
    """The emoji files of the name, left to right."""
    strip, spec = frames(name, palette)
    tiles = []
    for segment in range(spec.segments):
        left = segment * TILE
        part = [array[:, left : left + TILE] for array in strip]
        for crf in CRF_STEPS:
            data = encode([p.copy() for p in part], crf)
            if len(data) <= MAX_BYTES:
                break
        else:
            raise GlowError(f"segment {segment} is {len(data)} bytes")
        tiles.append(data)
    return tiles


def _preview_frame(array: Any, spec: Layout, gap: int = 2) -> Any:
    """One frame of the row on a dark background, the squares a little apart as Telegram shows them."""
    from PIL import Image

    width = spec.segments * TILE + (spec.segments - 1) * gap
    canvas = Image.new("RGBA", (width + 40, TILE + 40), (24, 25, 32, 255))
    for segment in range(spec.segments):
        piece = Image.fromarray(array[:, segment * TILE : (segment + 1) * TILE], "RGBA")
        canvas.alpha_composite(piece, (20 + segment * (TILE + gap), 20))
    return canvas.convert("RGB")


def preview_gif(name: str, palette: str = DEFAULT_PALETTE) -> bytes:
    """The shimmer as an animation (every other frame of the loop), to choose the colours."""
    from PIL import Image

    strip, spec = frames(name, palette)
    pictures = [
        _preview_frame(array, spec).convert("P", palette=Image.Palette.ADAPTIVE, colors=128)
        for array in strip[::2]
    ]
    buffer = io.BytesIO()
    pictures[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=pictures[1:],
        duration=round(1000 * SECONDS / len(pictures)),
        loop=0,
    )
    return buffer.getvalue()


def preview_png(name: str, palette: str = DEFAULT_PALETTE) -> bytes:
    """One frame of the row on a dark background, as the buyer will see it."""
    strip, spec = frames(name, palette)
    buffer = io.BytesIO()
    _preview_frame(strip[len(strip) // 3], spec).save(buffer, format="PNG")
    return buffer.getvalue()


def pack_name(service_id: int, version: str, bot_username: str) -> str:
    """t.me/addemoji/<name>: letters, digits and single underscores, ending with _by_<bot>."""
    return f"sl{service_id}g{version}_by_{bot_username}"


def new_version() -> str:
    """A pack name is never reused (a deleted one may stay taken for a while; a restored database would
    repeat a counter): the time in milliseconds since 2026 and two random characters, in base 36."""
    number = int((time.time() - 1_767_225_600) * 1000) * 1296 + secrets.randbelow(1296)
    digits = ""
    while number:
        number, rest = divmod(number, 36)
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"[rest] + digits
    return digits or "0"


async def publish(bot: Any, owner_id: int, set_name: str, title: str, name: str, palette: str) -> list[Glyph]:
    """Draw the name, upload it as the bot's custom emoji pack ``set_name`` (owned by ``owner_id``, a bot
    owner: nobody else can change the pack) and return its emoji, left to right."""
    from aiogram.types import BufferedInputFile, InputSticker

    tiles = await asyncio.to_thread(render, name, palette)
    stickers = [
        InputSticker(
            sticker=BufferedInputFile(data, filename=f"{index}.webm"), format="video", emoji_list=[ALT]
        )
        for index, data in enumerate(tiles)
    ]
    await bot.create_new_sticker_set(
        user_id=owner_id, name=set_name, title=title[:64], stickers=stickers, sticker_type="custom_emoji"
    )
    sticker_set = await bot.get_sticker_set(set_name)
    glyphs = [Glyph(s.custom_emoji_id, ALT) for s in sticker_set.stickers if s.custom_emoji_id]
    if len(glyphs) != len(tiles):
        raise GlowError(f"the pack has {len(glyphs)} emoji instead of {len(tiles)}")
    return glyphs
