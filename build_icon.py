"""Generate the macOS app icon (assets/AppIcon.icns) from a drawn 1024 master.

The Clay brand mark: a SOLID terracotta squircle (no gradient -- Clay forbids
them) with a centered white I-beam (text-cursor) mark, matching the design
system's brand-wordmark. Run with the venv python; requires Pillow + macOS
`iconutil`.
"""
import os
import subprocess
import sys

from PIL import Image, ImageDraw

S = 1024
CLAY = (194, 100, 63)         # clay-500 #C2643F -- the one accent, solid fill
WHITE = (255, 255, 255, 255)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Solid clay squircle (no gradient).
d.rounded_rectangle([80, 80, S - 80, S - 80], radius=220, fill=CLAY + (255,))

# Centered white I-beam mark: a vertical stem with top + bottom serifs -- the
# text-cursor glyph that is Clay's brand mark (edit the words inside a PDF).
cx = S // 2
stem_w, serif_w = 78, 230
top, bot = 300, 724
serif_h = 50
r = stem_w // 2
d.rounded_rectangle([cx - r, top, cx + r, bot], radius=r, fill=WHITE)        # stem
d.rounded_rectangle([cx - serif_w // 2, top, cx + serif_w // 2, top + serif_h],
                    radius=serif_h // 2, fill=WHITE)                          # top serif
d.rounded_rectangle([cx - serif_w // 2, bot - serif_h, cx + serif_w // 2, bot],
                    radius=serif_h // 2, fill=WHITE)                          # bottom serif

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
