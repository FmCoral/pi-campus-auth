# -*- coding: utf-8 -*-
"""生成 OLED 用 12px 点阵字模（在 Windows 电脑上运行，需要 Pillow）。

从系统字体（默认黑体 simhei.ttf）渲染指定字符，自动居中裁剪，
输出可直接粘贴进 src/oled_status.py 的字库代码，并打印字符画预览。

- 汉字：CJK 12x12 固定网格，每字 24 字节
- 英文/数字：ASCII 12 高变宽，每字符 [宽度, 上半字节..., 下半字节...]

用法：
    python gen_cjk_font.py [汉字集合]        # 缺省用脚本内置的常用集合
"""
import sys
from PIL import Image, ImageFont, ImageDraw, ImageFilter

DEFAULT_CHARS = '连接上行下行共网络温度内存通断'
ASCII_CHARS = ' 0123456789.ABCDEFGKLMNPTUs:/-%aelru'   # 与 src/oled_status.py 页面用到的字符对应
FONT_PATH = r'C:\Windows\Fonts\simhei.ttf'   # 黑体：笔画均匀，点阵屏上最清晰
FONT_PATH_A = r'C:\Windows\Fonts\arialbd.ttf'  # Arial Bold：英文数字笔画粗，屏上够醒目
SIZE = 12                                    # 汉字字模边长（像素）
SIZE_A = 14                                  # 英文/数字字模高度（像素）


def render_char(ch, font):
    """渲染单字到 24x24 画布，按笔画中心居中裁到 12x12，返回 bit 行主序列表。"""
    big = Image.new('1', (SIZE * 2, SIZE * 2), 0)
    d = ImageDraw.Draw(big)
    d.text((4, 4), ch, fill=1, font=font)
    bbox = big.getbbox()
    if not bbox:
        return None
    c = SIZE // 2
    cx, cy = (bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2
    ox, oy = cx - c, cy - c                     # 让笔画中心落到目标 (c,c)
    bits = []
    for y in range(SIZE):
        for x in range(SIZE):
            sx, sy = x + ox, y + oy
            on = 0 <= sx < SIZE * 2 and 0 <= sy < SIZE * 2 and big.getpixel((sx, sy))
            bits.append(1 if on else 0)
    return bits


def render_ascii(ch, font):
    """渲染 ASCII 字符，变宽裁剪：返回 (宽度, bit 行主序列表)。空格返回 (6, 全 0)。"""
    if ch == ' ':
        return 6, [0] * (SIZE_A * 6)
    big = Image.new('1', (SIZE_A * 2, SIZE_A * 2), 0)
    d = ImageDraw.Draw(big)
    d.text((4, 4), ch, fill=1, font=font)
    bbox = big.getbbox()
    if not bbox:
        return 6, [0] * (SIZE_A * 6)
    w = max(3, min(SIZE_A, bbox[2] - bbox[0]))
    cy = (bbox[1] + bbox[3]) // 2
    ox = (bbox[0] + bbox[2]) // 2 - w // 2
    oy = cy - SIZE_A // 2
    bits = []
    for y in range(SIZE_A):
        for x in range(w):
            sx, sy = x + ox, y + oy
            on = 0 <= sx < SIZE_A * 2 and 0 <= sy < SIZE_A * 2 and big.getpixel((sx, sy))
            bits.append(1 if on else 0)
    return w, bits


def to_bytes(bits, w, h):
    """bit 行主序 -> 显存列主序：g[x]=上半8行字节, g[x+w]=其余行。

    字节 bit0 = 顶行（与 SSD1306 页寻址方向一致）。"""
    g = [0] * (w * 2)
    for y in range(h):
        for x in range(w):
            if bits[y * w + x]:
                g[x if y < 8 else x + w] |= 1 << (y % 8)
    return g


def preview(bits, w, h):
    return [''.join('#' if bits[y * w + x] else '.' for x in range(w))
            for y in range(h)]


def main():
    chars = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CHARS
    font = ImageFont.truetype(FONT_PATH, SIZE)
    font_a = ImageFont.truetype(FONT_PATH_A, SIZE_A)
    out = ['CJK = {']
    for ch in chars:
        bits = render_char(ch, font)
        if bits is None:
            print(f'!! {ch} 渲染失败（无笔画）', file=sys.stderr)
            continue
        g = to_bytes(bits, SIZE, SIZE)
        hexs = ','.join(f'0x{b:02X}' for b in g)
        out.append(f"    '{ch}': [{hexs}],")
        print(f'--- {ch} ---')
        print('\n'.join(preview(bits, SIZE, SIZE)))
    out.append('}')
    out.append('')
    out.append('AFONT = {')
    for ch in ASCII_CHARS:
        w, bits = render_ascii(ch, font_a)
        g = to_bytes(bits, w, SIZE_A)
        hexs = ','.join(f'0x{b:02X}' for b in g)
        out.append(f"    '{ch}': [{w}, {hexs}],")
        if ch != ' ':
            print(f'--- {ch} (w={w}) ---')
            print('\n'.join(preview(bits, w, SIZE_A)))
    out.append('}')
    with open('cjk_font_snippet.py', 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')
    print('\n已写入 cjk_font_snippet.py')


if __name__ == '__main__':
    main()
