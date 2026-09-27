# -*- coding: utf-8 -*-
"""在电脑上预览 OLED 三页渲染效果（无需树莓派）。

把 src/oled_status.py 的 render() 输出转成字符画打印，用于验证
中英混排布局是否溢出、汉字字模是否正常。运行：python tools/preview_pages.py
"""
import os
import sys
import types

sys.modules.setdefault('fcntl', types.ModuleType('fcntl'))  # Windows 预览用桩
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import oled_status as o


def preview(buf):
    rows = []
    for page in range(8):
        for row in range(8):
            rows.append(''.join('#' if buf[page * 128 + x] & (1 << row) else '.'
                                for x in range(128)))
    return rows


PAGES = {
    '页面1 网络': ['连接 3', '上行 123KB/s', '下行 1.2MB/s', '共 1.2/0.3G'],
    '页面2 系统': ['网络 True', '温度 45.6C', '内存 35%', 'CPU 12%'],
}

for title, lines in PAGES.items():
    print(f'===== {title} : {lines} =====')
    for r in preview(o.render(lines)):
        print(r)
    print()
