import io
import math
import os
import tempfile
import subprocess
import logging

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Default font: try common system paths, fallback to Pillow default
_FONT_SEARCH_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/arial.ttf",
]


def _load_font(font_path: str, font_size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if font_path and os.path.isfile(font_path):
        return ImageFont.truetype(font_path, font_size)
    for path in _FONT_SEARCH_PATHS:
        if os.path.isfile(path):
            return ImageFont.truetype(path, font_size)
    return ImageFont.load_default(size=font_size)


def _hex_to_rgba(hex_color: str, opacity: float) -> tuple[int, int, int, int]:
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    a = int(opacity * 255)
    return r, g, b, a


def apply_watermark_to_image(
    image_bytes: bytes,
    text: str,
    font_size: int = 36,
    position: str = "center",
    opacity: float = 0.3,
    color: str = "#FFFFFF",
    rotation: int = 0,
    font_path: str = "",
) -> bytes:
    """Apply text watermark to an image. Returns watermarked image bytes (JPEG)."""
    base = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    font = _load_font(font_path, font_size)
    rgba_color = _hex_to_rgba(color, opacity)

    if position == "tiled":
        watermarked = _apply_tiled_watermark(base, text, font, rgba_color, rotation)
    else:
        watermarked = _apply_single_watermark(base, text, font, rgba_color, rotation, position)

    # Flatten to RGB for JPEG output
    output = Image.new("RGB", watermarked.size, (255, 255, 255))
    output.paste(watermarked, mask=watermarked.split()[3])

    buf = io.BytesIO()
    output.save(buf, format="JPEG", quality=95)
    buf.seek(0)
    return buf.getvalue()


def _get_text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _apply_single_watermark(
    base: Image.Image, text: str, font, color: tuple, rotation: int, position: str
) -> Image.Image:
    """Apply a single watermark at the specified position."""
    # Create text layer
    txt_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(txt_layer)
    tw, th = _get_text_size(draw, text, font)

    w, h = base.size
    padding = 20

    pos_map = {
        "center": ((w - tw) // 2, (h - th) // 2),
        "top-left": (padding, padding),
        "top-right": (w - tw - padding, padding),
        "bottom-left": (padding, h - th - padding),
        "bottom-right": (w - tw - padding, h - th - padding),
    }
    x, y = pos_map.get(position, ((w - tw) // 2, (h - th) // 2))

    if rotation != 0:
        # Draw on a larger canvas, rotate, then paste
        diag = int(math.sqrt(tw**2 + th**2)) + 40
        txt_piece = Image.new("RGBA", (diag, diag), (0, 0, 0, 0))
        d = ImageDraw.Draw(txt_piece)
        d.text(((diag - tw) // 2, (diag - th) // 2), text, fill=color, font=font)
        txt_piece = txt_piece.rotate(rotation, expand=False, resample=Image.BICUBIC)
        # Paste onto txt_layer at position
        paste_x = x - (diag - tw) // 2
        paste_y = y - (diag - th) // 2
        txt_layer.paste(txt_piece, (paste_x, paste_y), txt_piece)
    else:
        draw.text((x, y), text, fill=color, font=font)

    return Image.alpha_composite(base, txt_layer)


def _apply_tiled_watermark(
    base: Image.Image, text: str, font, color: tuple, rotation: int
) -> Image.Image:
    """Apply repeated watermark across the entire image."""
    txt_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(txt_layer)
    tw, th = _get_text_size(draw, text, font)

    if tw == 0 or th == 0:
        return base

    # Spacing between watermarks
    spacing_x = tw + max(tw, 100)
    spacing_y = th + max(th, 80)

    w, h = base.size
    # Draw extra tiles to cover after rotation
    margin = max(w, h)

    if rotation != 0:
        # Create a large tiled canvas, rotate it, then crop
        # Cap canvas size to prevent OOM on very large images
        canvas_size = min(int(math.sqrt(w**2 + h**2)) + margin, 8000)
        canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
        d = ImageDraw.Draw(canvas)
        for y_pos in range(-margin, canvas_size + margin, spacing_y):
            for x_pos in range(-margin, canvas_size + margin, spacing_x):
                d.text((x_pos, y_pos), text, fill=color, font=font)
        canvas = canvas.rotate(rotation, expand=False, resample=Image.BICUBIC)
        # Crop center to match base size
        cx, cy = canvas_size // 2, canvas_size // 2
        crop_box = (cx - w // 2, cy - h // 2, cx - w // 2 + w, cy - h // 2 + h)
        txt_layer = canvas.crop(crop_box)
    else:
        for y_pos in range(0, h, spacing_y):
            for x_pos in range(0, w, spacing_x):
                draw.text((x_pos, y_pos), text, fill=color, font=font)

    return Image.alpha_composite(base, txt_layer)


_ffmpeg_available: bool | None = None


def _check_ffmpeg() -> bool:
    global _ffmpeg_available
    if _ffmpeg_available is not None:
        return _ffmpeg_available
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        _ffmpeg_available = True
    except (FileNotFoundError, subprocess.CalledProcessError):
        _ffmpeg_available = False
    return _ffmpeg_available


def apply_watermark_to_video(
    video_bytes: bytes,
    text: str,
    font_size: int = 36,
    position: str = "center",
    opacity: float = 0.3,
    color: str = "#FFFFFF",
    rotation: int = 0,
    font_path: str = "",
) -> bytes | None:
    """Apply text watermark to video using ffmpeg. Returns None if ffmpeg not available.
    Note: rotation is not supported for video watermarks (ffmpeg drawtext limitation)."""
    if not _check_ffmpeg():
        logger.warning("ffmpeg not found, skipping video watermark")
        return None

    if rotation != 0:
        logger.info("Video watermark rotation is not supported, ignoring rotation=%d", rotation)

    hex_color = color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    # ffmpeg uses 0xRRGGBB@opacity format
    ff_color = f"0x{hex_color}@{opacity}"

    # Position mapping for ffmpeg drawtext
    pos_map = {
        "center": "x=(w-text_w)/2:y=(h-text_h)/2",
        "top-left": "x=20:y=20",
        "top-right": "x=w-text_w-20:y=20",
        "bottom-left": "x=20:y=h-text_h-20",
        "bottom-right": "x=w-text_w-20:y=h-text_h-20",
        "tiled": "x=20:y=20",  # tiled needs special handling below
    }
    pos_expr = pos_map.get(position, pos_map["center"])

    # Build fontfile arg
    ff_font = ""
    if font_path and os.path.isfile(font_path):
        ff_font = f":fontfile='{font_path}'"
    else:
        for path in _FONT_SEARCH_PATHS:
            if os.path.isfile(path):
                ff_font = f":fontfile='{path}'"
                break

    # Escape text for ffmpeg drawtext (must escape : \ ' ; % { })
    safe_text = text
    for ch in ("\\", "'", ":", ";", "%", "{", "}"):
        safe_text = safe_text.replace(ch, f"\\{ch}")

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_in:
        tmp_in.write(video_bytes)
        tmp_in_path = tmp_in.name

    tmp_out_path = tmp_in_path + "_wm.mp4"

    try:
        if position == "tiled":
            # For tiled: use multiple drawtext filters
            filters = []
            # Create a grid of watermarks (approximate, up to 5x5)
            for row in range(5):
                for col in range(5):
                    x_expr = f"x=w*{col}/5+20"
                    y_expr = f"y=h*{row}/5+20"
                    filters.append(
                        f"drawtext=text='{safe_text}':fontsize={font_size}"
                        f":fontcolor={ff_color}{ff_font}:{x_expr}:{y_expr}"
                    )
            filter_str = ",".join(filters)
        else:
            filter_str = (
                f"drawtext=text='{safe_text}':fontsize={font_size}"
                f":fontcolor={ff_color}{ff_font}:{pos_expr}"
            )

        cmd = [
            "ffmpeg", "-y", "-i", tmp_in_path,
            "-vf", filter_str,
            "-codec:a", "copy",
            "-preset", "fast",
            tmp_out_path,
        ]

        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            logger.error("ffmpeg failed: %s", result.stderr.decode(errors="replace")[:500])
            return None

        with open(tmp_out_path, "rb") as f:
            return f.read()
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out processing video watermark")
        return None
    except Exception as e:
        logger.error("Video watermark error: %s", e)
        return None
    finally:
        for p in (tmp_in_path, tmp_out_path):
            try:
                os.unlink(p)
            except OSError:
                pass
