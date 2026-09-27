"""The glowing name: 100×100 VP9 emoji with alpha, within Telegram's limits, one row per name."""

from __future__ import annotations

import io
import re

import pytest

from app.services import glow

pytestmark = pytest.mark.skipif(not glow.available(), reason="Pillow / PyAV / a font are not installed")


def _alpha(data: bytes) -> list:
    """The alpha plane of every frame as the libvpx decoder gives it (read directly: converting a 100-pixel
    wide yuva420p frame to RGBA smears the right edge in swscale, which a player never does)."""
    import av
    import numpy as np

    container = av.open(io.BytesIO(data))
    stream = container.streams.video[0]
    assert stream.codec_context.name == "vp9" and stream.metadata.get("alpha_mode") == "1"
    decoder = av.CodecContext.create("libvpx-vp9", "r")
    planes = []
    for packet in container.demux(stream):
        for frame in decoder.decode(packet):
            assert (frame.format.name, frame.width, frame.height) == ("yuva420p", glow.TILE, glow.TILE)
            plane = frame.planes[3]
            rows = np.frombuffer(bytes(plane), np.uint8).reshape(plane.height, plane.line_size)
            planes.append(rows[:, : plane.width])
    return planes


def test_a_name_becomes_a_row_of_video_emoji_within_telegram_limits():
    import numpy as np

    tiles = glow.render("Tripmafia", "neon")
    strip, spec = glow.frames("Tripmafia", "neon")
    assert len(tiles) == spec.segments and glow.MIN_SEGMENTS <= len(tiles) <= glow.MAX_SEGMENTS
    for segment, data in enumerate(tiles):
        assert len(data) <= glow.MAX_BYTES
        planes = _alpha(data)
        assert len(planes) == glow.FPS * glow.SECONDS  # a 2-second loop at 30 fps (Telegram: ≤ 3 s, ≤ 30)
        for drawn, decoded in zip(strip, planes, strict=True):
            source = drawn[:, segment * glow.TILE : (segment + 1) * glow.TILE, 3]
            assert np.abs(decoded.astype(int) - source).max() <= 16  # no seams, no background creeping in
    middle = _alpha(tiles[len(tiles) // 2])[0]
    assert (middle == 0).mean() > 0.1 and (middle > 200).mean() > 0.05  # letters on a transparent ground


def test_short_names_take_fewer_squares_and_long_ones_are_capped():
    short, long = glow.layout("VPN"), glow.layout("Очень длинное название сервиса номер один")
    assert short.segments < long.segments == glow.MAX_SEGMENTS
    assert long.size < glow.TEXT_SIZE  # smaller letters rather than more squares


def test_pack_names_follow_telegram_rules_and_preview_is_a_png():
    name = glow.pack_name(12, glow.new_version(), "servicelist_bot")
    assert glow.new_version() != glow.new_version()
    assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) and "__" not in name
    assert name.endswith("_by_servicelist_bot")
    assert glow.preview_png("Tripmafia", "gold").startswith(b"\x89PNG")


def test_only_what_the_font_can_draw_is_drawn():
    assert glow.drawable("🔥 Tripmafia 🔥") == "Tripmafia"  # colour emoji would be empty boxes
    assert glow.drawable("Кофе ☕ 24/7") == "Кофе ☕ 24/7"
    assert glow.drawable("👍👍") == ""
    with pytest.raises(glow.GlowError):
        glow.layout("")


def test_the_preview_is_an_animation():
    from PIL import Image

    image = Image.open(io.BytesIO(glow.preview_gif("VPN", "rainbow")))
    assert image.format == "GIF" and image.n_frames == glow.FPS * glow.SECONDS // 2
