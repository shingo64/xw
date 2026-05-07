#!/usr/bin/env python3
"""シンプルなPWAアイコン生成（PIL）。"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

OUT = Path(__file__).parent / "app" / "icons"
OUT.mkdir(parents=True, exist_ok=True)


def make(size: int, maskable: bool, fname: str):
    img = Image.new("RGBA", (size, size), (11, 61, 46, 255))  # theme color
    d = ImageDraw.Draw(img)
    # マスカブル用は中央60%にコンテンツを収める
    inner_pad = int(size * 0.20) if maskable else int(size * 0.06)
    inner = (inner_pad, inner_pad, size - inner_pad, size - inner_pad)
    # 走者の模式アイコン: 緑の円 + "XW100"テキスト
    cx, cy = size / 2, size / 2
    r = (size - inner_pad * 2) / 2 * 0.95
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(22, 128, 88, 255))
    # テキスト
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", int(size * 0.22))
        font_sub = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", int(size * 0.13))
    except Exception:
        font = ImageFont.load_default()
        font_sub = font
    text = "XW"
    sub = "100"
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text((cx - tw / 2 - bbox[0], cy - th * 0.85 - bbox[1]), text, fill=(232, 239, 233, 255), font=font)
    bbox2 = d.textbbox((0, 0), sub, font=font_sub)
    sw_, sh = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]
    d.text((cx - sw_ / 2 - bbox2[0], cy + th * 0.05 - bbox2[1]), sub, fill=(93, 211, 158, 255), font=font_sub)
    img.save(OUT / fname)
    print(f"wrote {OUT / fname} ({size}x{size}, maskable={maskable})")


def main():
    make(192, False, "icon-192.png")
    make(512, False, "icon-512.png")
    make(512, True,  "icon-maskable-512.png")
    # SVGも一応
    svg = """<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" fill="#0b3d2e"/>
  <circle cx="256" cy="256" r="220" fill="#168058"/>
  <text x="256" y="245" font-family="Arial,sans-serif" font-weight="bold" font-size="120" fill="#e8efe9" text-anchor="middle" dominant-baseline="middle">XW</text>
  <text x="256" y="345" font-family="Arial,sans-serif" font-weight="bold" font-size="72"  fill="#5dd39e" text-anchor="middle" dominant-baseline="middle">100</text>
</svg>"""
    (OUT / "icon.svg").write_text(svg)
    print(f"wrote {OUT / 'icon.svg'}")


if __name__ == "__main__":
    main()
