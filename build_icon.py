"""Generate the macOS app icon (assets/AppIcon.icns) from a drawn 1024 master.

A blue rounded-square (squircle-ish) with a white document page, grey text
lines, and an accent text-edit caret -- reads as a text/PDF editor. Run with
the venv python; requires Pillow + macOS `iconutil`.
"""
import os
import subprocess
import sys

from PIL import Image, ImageDraw

S = 1024
ACCENT = (10, 102, 255)
ACCENT_DARK = (7, 78, 200)
CARET = (224, 64, 64)
INK = (60, 62, 66)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Background squircle (vertical accent gradient on a rounded square).
bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
bgd = ImageDraw.Draw(bg)
for y in range(S):
    t = y / S
    r = int(ACCENT[0] * (1 - t) + ACCENT_DARK[0] * t)
    g = int(ACCENT[1] * (1 - t) + ACCENT_DARK[1] * t)
    b = int(ACCENT[2] * (1 - t) + ACCENT_DARK[2] * t)
    bgd.line([(0, y), (S, y)], fill=(r, g, b, 255))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle(
    [80, 80, S - 80, S - 80], radius=200, fill=255)
img.paste(bg, (0, 0), mask)
d = ImageDraw.Draw(img)

# White document page with a soft shadow.
px0, py0, px1, py1 = 300, 250, 724, 800
shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(shadow).rounded_rectangle(
    [px0 + 14, py0 + 22, px1 + 14, py1 + 22], radius=34, fill=(0, 0, 0, 70))
img.alpha_composite(shadow)
d.rounded_rectangle([px0, py0, px1, py1], radius=34, fill=(255, 255, 255, 255))

# Text lines on the page.
lx0 = px0 + 70
lw = (px1 - px0) - 140
line_ys = [py0 + 130, py0 + 230, py0 + 330, py0 + 430]
widths = [1.0, 0.82, 0.92, 0.6]
for y, wf in zip(line_ys, widths):
    d.rounded_rectangle([lx0, y, lx0 + int(lw * wf), y + 34],
                        radius=17, fill=(200, 204, 210, 255))

# Accent text-edit caret over the last line (the "editing" cue).
cx = lx0 + int(lw * 0.6) + 26
cy0, cy1 = py0 + 408, py0 + 472
d.rectangle([cx - 5, cy0, cx + 5, cy1], fill=CARET)
d.rectangle([cx - 18, cy0, cx + 18, cy0 + 10], fill=CARET)
d.rectangle([cx - 18, cy1 - 10, cx + 18, cy1], fill=CARET)

os.makedirs("assets", exist_ok=True)
img.save("assets/icon_1024.png")

# Windows icon (.ico): one multi-size icon Pillow writes on ANY platform, so the
# Windows CI runner produces it without macOS tooling. The spec points the
# Windows EXE at assets/AppIcon.ico.
ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
             (128, 128), (256, 256)]
img.save("assets/AppIcon.ico", format="ICO", sizes=ico_sizes)
print("wrote assets/AppIcon.ico")

# macOS icon (.icns): the .iconset -> .icns conversion needs Apple's `iconutil`,
# so this half runs on macOS only. On Windows the .ico above is all that is used.
if sys.platform == "darwin":
    iconset = "assets/AppIcon.iconset"
    os.makedirs(iconset, exist_ok=True)
    specs = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2),
             (256, 1), (256, 2), (512, 1), (512, 2)]
    for size, scale in specs:
        px = size * scale
        name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
        img.resize((px, px), Image.LANCZOS).save(os.path.join(iconset, name))
    subprocess.run(["iconutil", "-c", "icns", iconset,
                    "-o", "assets/AppIcon.icns"], check=True)
    print("wrote assets/AppIcon.icns")
