# -*- coding: utf-8 -*-
"""生成应用图标：深色圆角方块 + 「卅」字，输出多尺寸 .ico。"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent
OUT = ROOT / "assets" / "sha.ico"
OUT.parent.mkdir(exist_ok=True)

SIZE = 256
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 背景方块（DeepSeek 蓝）
d.rectangle([8, 8, SIZE - 8, SIZE - 8], fill=(15, 15, 15, 255))
d.rectangle([8, 8, SIZE - 8, SIZE - 8], outline=(77, 107, 254, 255), width=10)

# 「卅」字
font = None
for name in ("msyhbd.ttc", "msyh.ttc", "simhei.ttf", "simsun.ttc"):
    try:
        font = ImageFont.truetype(name, 150)
        break
    except OSError:
        continue
if font is None:
    raise SystemExit("找不到可用的中文字体")

w, h = font.getsize("卅")
d.text(((SIZE - w) / 2, (SIZE - h) / 2), "卅", font=font, fill=(77, 107, 254, 255))

img.save(OUT, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("OK", OUT)
