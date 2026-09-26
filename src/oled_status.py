#!/usr/bin/env python3
# OLED 状态屏 - HWIFI 实时监控（硬件 I2C + 常驻守护进程，0.5s 刷新，每页 1s）
#
# 接线：VCC->引脚1(3.3V)  GND->引脚9  SDA->引脚3(GPIO2)  SCL->引脚5(GPIO3)
# 页面1（网络）：
#   L: 3          连接数
#   U: 123KB/s    上传速度
#   D: 1.2MB/s    下载速度
#   1.2/0.3 G     整机累计流量
# 页面2（系统）：
#   NET: T        校园网连通（ping 223.5.5.5）
#   TEM: 45.6C    CPU 温度
#   MEM: 35%      内存使用率
#   CPU: 12%      CPU 使用率
# 页面3（设备）：
#   vivo 1.2G     设备名(取 DHCP hostname 前4字符) + 累计下载+上传流量
#
# 按设备记账：守护进程每 5s 直接对账 nft 表 inet hwacct
#   - 按 /tmp/dhcp.leases 增删计数器/规则（不依赖 dnsmasq dhcpscript，绕过 ujail）
#   - 读计数器差值 -> 按 MAC 累计（换 IP 不丢账）
#   状态文件：/root/traffic_state.json（设备累计，跨重启）
#   每 10 分钟写一行快照到 /root/traffic_log/YYYY-MM-DD.txt
# 部署：procd 服务 /etc/init.d/oled

import fcntl, os, time, subprocess, json, datetime, ipaddress, re

I2C_BUS = '/dev/i2c-1'
I2C_ADDR = 0x3C
STATE_FILE = '/root/.oled_state'           # 整机累计
DEV_STATE = '/root/traffic_state.json'     # 设备累计 {mac: {"name":..,"rx":..,"tx":..}}
LOG_DIR = '/root/traffic_log'
LEASES = '/tmp/dhcp.leases'
WAN_IF = 'eth0'
REFRESH = 0.5
PROBE_HOST = '223.5.5.5'

FONT = {
    ' ': [0x00,0x00,0x00,0x00,0x00], '0': [0x3E,0x51,0x49,0x45,0x3E],
    '1': [0x00,0x42,0x7F,0x40,0x00], '2': [0x42,0x61,0x51,0x49,0x46],
    '3': [0x21,0x41,0x45,0x4B,0x31], '4': [0x18,0x14,0x12,0x7F,0x10],
    '5': [0x27,0x45,0x45,0x45,0x39], '6': [0x3C,0x4A,0x49,0x49,0x30],
    '7': [0x01,0x71,0x09,0x05,0x03], '8': [0x36,0x49,0x49,0x49,0x36],
    '9': [0x06,0x49,0x49,0x29,0x1E], '.': [0x00,0x60,0x60,0x00,0x00],
    'A': [0x7E,0x11,0x11,0x11,0x7E], 'B': [0x7F,0x49,0x49,0x49,0x36],
    'C': [0x3E,0x41,0x41,0x41,0x22], 'D': [0x7F,0x41,0x41,0x22,0x1C],
    'E': [0x7F,0x49,0x49,0x49,0x41], 'F': [0x7F,0x09,0x09,0x09,0x01],
    'G': [0x3E,0x41,0x49,0x49,0x7A], 'K': [0x7F,0x08,0x14,0x22,0x41],
    'L': [0x7F,0x40,0x40,0x40,0x40], 'M': [0x7F,0x02,0x0C,0x02,0x7F],
    'N': [0x7F,0x04,0x08,0x10,0x7F], 'P': [0x7F,0x09,0x09,0x09,0x06],
    'T': [0x01,0x01,0x7F,0x01,0x01], 'U': [0x3F,0x40,0x40,0x40,0x3F],
    's': [0x46,0x49,0x49,0x49,0x31], ':': [0x00,0x36,0x36,0x00,0x00],
    '/': [0x20,0x10,0x08,0x04,0x02], '-': [0x08,0x08,0x08,0x08,0x08],
    '%': [0x23,0x13,0x08,0x64,0x62],
}


def render(lines):
    buf = bytearray(1024)
    def pixel(x, y):
        if 0 <= x < 128 and 0 <= y < 64:
            buf[(y // 8) * 128 + x] |= 1 << (y % 8)
    def char2x(x, y, ch):
        for col, bits in enumerate(FONT.get(ch, FONT[' '])):
            for row in range(7):
                if bits & (1 << row):
                    for dx in (0, 1):
                        for dy in (0, 1):
                            pixel(x + col * 2 + dx, y + row * 2 + dy)
    for i, line in enumerate(lines):
        for j, ch in enumerate(line[:11]):
            char2x(j * 11, i * 16, ch)
    return buf


class OLED:
    def __init__(self):
        self.fd = os.open(I2C_BUS, os.O_RDWR)
        fcntl.ioctl(self.fd, 0x0703, I2C_ADDR)
        for c in [0xAE, 0xD5,0x80, 0xA8,0x3F, 0xD3,0x00, 0x40,
                  0x8D,0x14, 0x20,0x00, 0xA1, 0xC8, 0xDA,0x12,
                  0x81,0xCF, 0xD9,0xF1, 0xDB,0x40, 0xA4, 0xA6,
                  0x21,0,127, 0x22,0,7, 0xAF]:
            os.write(self.fd, bytes([0x00, c]))

    def show(self, buf):
        os.write(self.fd, bytes([0x40]) + bytes(buf))


def get_clients():
    try:
        r = subprocess.run(['iw', 'dev', 'phy0-ap0', 'station', 'dump'],
                           capture_output=True, timeout=5)
        return r.stdout.count(b'Station ')
    except Exception:
        return -1


def check_online():
    try:
        r = subprocess.run(['ping', '-c', '1', '-W', '1', PROBE_HOST],
                           capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def get_net_bytes():
    b = f'/sys/class/net/{WAN_IF}/statistics'
    return int(open(b + '/rx_bytes').read()), int(open(b + '/tx_bytes').read())


def get_temp():
    try:
        return int(open('/sys/class/thermal/thermal_zone0/temp').read()) / 1000
    except Exception:
        return 0.0


def get_mem_pct():
    total = avail = 0
    for line in open('/proc/meminfo'):
        if line.startswith('MemTotal:'):
            total = int(line.split()[1])
        elif line.startswith('MemAvailable:'):
            avail = int(line.split()[1])
    return (total - avail) * 100 // total if total else 0


def read_cpu():
    p = open('/proc/stat').readline().split()[1:]
    v = list(map(int, p))
    return v[3] + v[4], sum(v)


def load_total():
    try:
        p = open(STATE_FILE).read().split()
        return int(p[0]), int(p[1])
    except Exception:
        return 0, 0


def save_total(rx, tx):
    with open(STATE_FILE, 'w') as f:
        f.write(f'{rx} {tx}')


def fmt_speed(bps):
    kb = bps / 1024
    return f'{int(kb)}KB/s' if kb < 1024 else f'{kb / 1024:.1f}MB/s'


def fmt_gb(b):
    return f'{b / 1073741824:.1f}'


# ---------- 设备记账 ----------
def read_leases():
    """返回 {ip: (mac, hostname)}"""
    out = {}
    try:
        for line in open(LEASES):
            p = line.split()
            if len(p) >= 4:
                out[p[2]] = (p[1], p[3])
    except Exception:
        pass
    return out


def load_dev_state():
    """返回 (devs, baselines)，结构 {"devs": {...}, "base": {h: {up,down}}}"""
    try:
        d = json.load(open(DEV_STATE))
        return d.get('devs', {}), d.get('base', {})
    except Exception:
        return {}, {}


def save_dev_state(devs, base):
    tmp = DEV_STATE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({'devs': devs, 'base': base}, f)
    os.replace(tmp, DEV_STATE)


def read_nft_counters():
    """返回 {iphex: {"up": bytes, "down": bytes}}，计数器累计值"""
    try:
        r = subprocess.run(['nft', '-j', 'list', 'table', 'inet', 'hwacct'],
                           capture_output=True, timeout=8)
        data = json.loads(r.stdout)
        res = {}
        for obj in data.get('nftables', []):
            ctr = obj.get('counter')
            if ctr and 'name' in ctr:
                name = ctr['name']
                if name.startswith('up_'):
                    h = name[3:]
                elif name.startswith('down_'):
                    h = name[5:]
                else:
                    continue
                d = res.setdefault(h, {'up': 0, 'down': 0})
                d['up' if name.startswith('up_') else 'down'] = int(ctr.get('bytes', 0))
        return res
    except Exception:
        return {}


def h_to_ip(h):
    return str(ipaddress.IPv4Address(bytes.fromhex(h)))


def ip_to_h(ip):
    return ipaddress.IPv4Address(ip).packed.hex()


def nft_run(args):
    try:
        subprocess.run(['nft'] + args, capture_output=True, timeout=8)
    except Exception:
        pass


def reconcile_nft(leases):
    """按租约对账 nft 计数器/规则：新增的设备建计数器，离开的删掉"""
    nft_run(['add', 'table', 'inet', 'hwacct'])
    nft_run(['add', 'chain', 'inet', 'hwacct', 'acct',
             '{ type filter hook forward priority -190; policy accept; }'])

    r = subprocess.run(['nft', '-a', 'list', 'chain', 'inet', 'hwacct', 'acct'],
                       capture_output=True, timeout=8)
    text = r.stdout.decode(errors='replace')
    existing = {}                       # hex -> [handle,...]
    for line in text.splitlines():
        m = re.search(r'name "(?:up|down)_([0-9a-f]+)"', line)
        h2 = re.search(r'# handle (\d+)', line)
        if m and h2:
            existing.setdefault(m.group(1), []).append(int(h2.group(1)))

    wanted = {ip_to_h(ip) for ip in leases}

    # 新增
    for h in wanted - set(existing):
        ip = h_to_ip(h)
        nft_run(['add', 'counter', 'inet', 'hwacct', f'up_{h}'])
        nft_run(['add', 'counter', 'inet', 'hwacct', f'down_{h}'])
        nft_run(['add', 'rule', 'inet', 'hwacct', 'acct',
                 'ip', 'saddr', ip, 'counter', 'name', f'up_{h}'])
        nft_run(['add', 'rule', 'inet', 'hwacct', 'acct',
                 'ip', 'daddr', ip, 'counter', 'name', f'down_{h}'])

    # 删除已离开设备：先按 handle 删规则，再删计数器
    for h in set(existing) - wanted:
        for handle in existing[h]:
            nft_run(['delete', 'rule', 'inet', 'hwacct', 'acct', 'handle', str(handle)])
        nft_run(['delete', 'counter', 'inet', 'hwacct', f'up_{h}'])
        nft_run(['delete', 'counter', 'inet', 'hwacct', f'down_{h}'])


def write_daily_log(devs):
    """每 10 分钟一行设备快照"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, f'{datetime.date.today().isoformat()}.txt')
        ts = time.strftime('%H:%M')
        parts = '  '.join(f'{v["name"]}={fmt_gb(v["rx"]+v["tx"])}G'
                          for v in sorted(devs.values(), key=lambda x: x['rx']+x['tx'], reverse=True))
        with open(path, 'a') as f:
            f.write(f'[{ts}] {parts}\n')
    except Exception:
        pass


def run():
    oled = OLED()
    total_rx, total_tx = load_total()
    last_rx, last_tx = get_net_bytes()
    last_t = time.time()
    last_idle, last_cpu = read_cpu()
    clients = get_clients()
    online = check_online()

    devs, base = load_dev_state()
    prev_ctrs = base if base else read_nft_counters()

    n = 0
    while True:
        time.sleep(REFRESH)
        # --- 整机速度/累计 ---
        rx, tx = get_net_bytes()
        now = time.time()
        dt = max(now - last_t, 0.01)
        drx = rx - last_rx if rx >= last_rx else 0
        dtx = tx - last_tx if tx >= last_tx else 0
        down, up = drx / dt, dtx / dt
        total_rx += drx; total_tx += dtx
        last_rx, last_tx, last_t = rx, tx, now

        idle, cputotal = read_cpu()
        di, dtotal = idle - last_idle, cputotal - last_cpu
        cpu_pct = (dtotal - di) * 100 // dtotal if dtotal > 0 else 0
        last_idle, last_cpu = idle, cputotal

        # --- 设备级累计（每 5s）---
        n += 1
        if n % 10 == 0:
            leases = read_leases()
            reconcile_nft(leases)
            ctrs = read_nft_counters()
            for h, cur in ctrs.items():
                prev = prev_ctrs.get(h, {'up': cur['up'], 'down': cur['down']})
                d_up = cur['up'] - prev['up']
                d_down = cur['down'] - prev['down']
                ip = h_to_ip(h)
                mac, host = leases.get(ip, ('', ''))
                # 计数器首次出现（新规则）时，基准值就是当前值，不计历史
                if h not in prev_ctrs or (d_up < 0 or d_down < 0):
                    prev_ctrs[h] = cur
                    continue
                if not mac:
                    continue
                e = devs.setdefault(mac, {'name': host or mac[-5:], 'rx': 0, 'tx': 0})
                if host and (not e['name'] or e['name'] == mac[-5:]):
                    e['name'] = host
                e['rx'] += d_down
                e['tx'] += d_up
                prev_ctrs[h] = cur
            # 消失的计数器从基准表清掉
            for h in list(prev_ctrs):
                if h not in ctrs:
                    del prev_ctrs[h]

        if n % 20 == 1:
            clients = get_clients()
        if n % 20 == 11:
            online = check_online()
        if n % 120 == 0:
            save_total(total_rx, total_tx)
            save_dev_state(devs, prev_ctrs)
        if n % 1200 == 0:                              # 每 10 分钟写日志
            write_daily_log(devs)

        page = (n // 2) % 3
        if page == 0:
            lines = [
                f'L: {clients}',
                f'U: {fmt_speed(up)}',
                f'D: {fmt_speed(down)}',
                f'{fmt_gb(total_rx)}/{fmt_gb(total_tx)} G',
            ]
        elif page == 1:
            lines = [
                f'NET: {"T" if online else "F"}',
                f'TEM: {get_temp():.1f}C',
                f'MEM: {get_mem_pct()}%',
                f'CPU: {cpu_pct}%',
            ]
        else:
            top = sorted(devs.values(), key=lambda v: v['rx']+v['tx'], reverse=True)[:4]
            if not top:
                lines = ['NO DATA', '', '', '']
            else:
                lines = []
                for v in top:
                    nm = (v['name'][:4] or '????')
                    lines.append(f'{nm} {fmt_gb(v["rx"]+v["tx"])}G')
                while len(lines) < 4:
                    lines.append('')
        oled.show(render(lines))


def main():
    while True:
        try:
            run()
        except Exception:
            time.sleep(2)


if __name__ == '__main__':
    main()
