#!/usr/bin/env python3
# 关机善后脚本 - 由定时任务(cron 23:20)或面板「立即关机」调用
#
# 流程：停 OLED 服务（SIGTERM 触发整机/设备累计末次落盘）
#       → OLED 居中显示"已关机"（看到即可安全拔掉电源）
#       → sync 文件系统 → poweroff 优雅关机
#
# 部署位置：/root/do_shutdown.py

import sys, time, os, subprocess
sys.path.insert(0, '/root')
import oled_status

LOGF = '/root/shutdown.log'


def log(msg):
    line = '[%s] %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    print(line, flush=True)
    try:
        with open(LOGF, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def show_powered_off(oled):
    """OLED 居中显示 12px"已关机"，返回显存缓冲"""
    text = '已关机'
    w = len(text) * 12
    x0 = (128 - w) // 2
    y0 = 26
    buf = bytearray(1024)
    for i, ch in enumerate(text):
        g = oled_status.CJK[ch]
        for col in range(12):
            lo, hi = g[col], g[col + 12]
            x = x0 + i * 12 + col
            for row in range(12):
                b = lo if row < 8 else hi
                y = y0 + row
                if b & (1 << (row % 8)) and 0 <= y < 64 and 0 <= x < 128:
                    buf[(y // 8) * 128 + x] |= 1 << (y % 8)
    oled.show(buf)


def main():
    log('shutdown sequence start')

    # 1. 停 OLED：SIGTERM 处理器做末次数据落盘（.oled_state + traffic_state.json）
    r = subprocess.run(['/etc/init.d/oled', 'stop'], timeout=30)
    log('oled stopped rc=%d' % r.returncode)
    time.sleep(0.5)

    # 2. 接管 OLED，显示"已关机"
    oled = oled_status.OLED()
    show_powered_off(oled)
    log('oled shows 已关机')

    # 3. sync：把缓冲写入 SD 卡
    subprocess.run(['sync'], timeout=10)
    time.sleep(0.5)
    subprocess.run(['sync'], timeout=10)
    log('sync done, calling poweroff')

    # 4. 优雅关机（procd 停服务、卸载文件系统；OLED 保持"已关机"画面直到断电）
    if os.environ.get('DO_SHUTDOWN_DRY'):
        log('DRY mode: skip poweroff')
    else:
        os.system('poweroff')


if __name__ == '__main__':
    main()
