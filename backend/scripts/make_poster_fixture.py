"""生成一张模拟华为运动海报的测试图，用于验证 OCR 识别链路。

真实海报数据（来自用户 9.05 户外跑步 summary）：
  户外跑步 / 2026-09-05 20:24 / 5.68 km / 00:34:51 / 平均心率170 最大181
  平均步频167 / 步幅97cm / 累计爬升15m / 累计下降17m / 卡路里454

用中文字体渲染成一张带渐变的图，尽量贴近华为分享海报的版式。
"""
from PIL import Image, ImageDraw, ImageFont
import os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "poster_fixture.png")

W, H = 720, 1280
img = Image.new("RGB", (W, H))
draw = ImageDraw.Draw(img)

# 渐变背景（上深下浅，模拟海报）
for y in range(H):
    t = y / H
    r = int(24 + (1 - t) * 40)
    g = int(30 + (1 - t) * 55)
    b = int(52 + (1 - t) * 70)
    draw.line([(0, y), (W, y)], fill=(r, g, b))

# 找中文字体
def font(size):
    for p in [
        "C:/Windows/Fonts/msyh.ttc",       # 微软雅黑
        "C:/Windows/Fonts/simhei.ttf",     # 黑体
        "C:/Windows/Fonts/simsun.ttc",     # 宋体
    ]:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()

f_title = font(64)
f_big = font(88)
f_mid = font(44)
f_small = font(32)

white = (255, 255, 255)
cyan = (120, 220, 255)

# 顶部：运动类型
draw.text((60, 80), "户外跑步", font=f_title, fill=white)

# 大数字：距离
draw.text((60, 220), "5.68", font=ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 140), fill=cyan)
draw.text((430, 320), "公里", font=f_mid, fill=white)

# 时长
draw.text((60, 440), "00:34:51", font=f_big, fill=white)
draw.text((430, 480), "时长", font=f_small, fill=(200, 200, 200))

# 数据网格
rows = [
    ("平均心率", "170", "次/分"),
    ("最大心率", "181", "次/分"),
    ("平均步频", "167", "步/分"),
    ("步幅", "97", "厘米"),
    ("累计爬升", "15", "米"),
    ("累计下降", "17", "米"),
    ("卡路里", "454", "千卡"),
]
yy = 620
for label, val, unit in rows:
    draw.text((60, yy), label, font=f_mid, fill=(200, 200, 200))
    draw.text((330, yy - 8), val, font=f_big, fill=white)
    draw.text((560, yy + 20), unit, font=f_small, fill=(180, 180, 180))
    yy += 90

# 底部日期
draw.text((60, H - 120), "2026年9月5日 20:24", font=f_mid, fill=white)

img.save(OUT)
print("已生成测试图:", OUT)
