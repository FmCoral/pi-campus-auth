#!/usr/bin/env python3
# Web 监控面板 - 单文件 HTTP 服务（python3 标准库，零依赖）
#
#   页面  http://192.168.1.1:8080/            → monitor.html（每次请求现读，改完即生效）
#   接口  /api/status                          → 总览/设备/系统（2s 轮询）
#         /api/hist?h=24|72                    → 分钟级流量历史（3 天环形缓冲）
#         /api/history?days=30                 → 按天流量（traffic_log 差值估算）
#         /api/log?lines=200                   → 认证日志尾部
#
# 数据复用 oled_status.py 的采集函数（同目录 import，单一数据源）。
# 后台线程每 5s 采样；分钟流量点每 60s 一个，持久化 /root/traffic_hist.json
# （默认保留 3 天，每 2 分钟原子落盘一次，重启/关机最多丢 2 分钟）。
# 部署：/root/monitor_web.py + /root/monitor.html，procd 服务 /etc/init.d/monitorweb
# 防火墙：wan 区 input REJECT，校园网侧访问不到；仅 LAN 可看。

import json, os, re, sys, time, threading, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from oled_status import (read_leases, load_dev_state, read_nft_counters, h_to_ip,
                         ip_to_h, get_temp, get_mem_pct, read_cpu, get_net_bytes,
                         check_online, get_clients, load_total, LOG_DIR)

PORT = 8080
HTML_PATH = os.path.join(BASE, 'monitor.html')
AUTH_LOG = '/root/auto_auth.log'
HIST_FILE = '/root/traffic_hist.json'
HIST_DAYS = 3                # 分钟级历史保留天数
HIST_MAX = HIST_DAYS * 1440  # 4320 点
TREND_MAX = 60               # 5 分钟趋势（5s 一个点）
SAMPLER_EVERY = 5            # 采样周期（秒）
LINK_CAP = 125_000_000       # 1Gbps 线速；超过视为计数器异常，钳制（防坏点破坏图表缩放）

LOCK = threading.Lock()
STATE = {
    'trend': [],            # [{'t': epoch, 'up': B/s, 'down': B/s}] 最近 5 分钟
    'hist': [],             # 分钟级 [{'t','up','down'}] 最多 3 天
    'online': False,
    'clients': 0,
    'wan_ip': '',
    'gateway': '',
    'cpu_pct': 0,
    'dev_rate': {},         # iphex: {'up': B/s, 'down': B/s}
    'leases': {},           # {ip: (mac, hostname)}
    'services': [],         # [{'name','running'}]
}


def load_hist():
    """启动时恢复分钟级历史，丢弃超期点并清洗超线速坏点"""
    try:
        pts = json.load(open(HIST_FILE))
        cutoff = time.time() - HIST_DAYS * 86400
        out = []
        for p in pts:
            if p.get('t', 0) < cutoff:
                continue
            if p.get('up', 0) > LINK_CAP or p.get('down', 0) > LINK_CAP:
                continue                      # 历史坏点直接剔除
            out.append(p)
        return out[-HIST_MAX:]
    except Exception:
        return []


def save_hist():
    tmp = HIST_FILE + '.tmp'
    with LOCK:
        pts = list(STATE['hist'])
    try:
        with open(tmp, 'w') as f:
            json.dump(pts, f)
        os.replace(tmp, HIST_FILE)
    except Exception:
        pass


def get_gateway():
    try:
        r = subprocess.run(['ip', 'route', 'show', 'default'],
                           capture_output=True, timeout=5)
        m = re.search(r'via (\d+\.\d+\.\d+\.\d+)', r.stdout.decode())
        return m.group(1) if m else ''
    except Exception:
        return ''


def get_wan_ip():
    try:
        r = subprocess.run(['ip', '-4', 'addr', 'show', 'dev', 'eth0'],
                           capture_output=True, timeout=5)
        m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', r.stdout.decode())
        return m.group(1) if m else ''
    except Exception:
        return ''


def get_services():
    """关键服务健康：进程在即视为运行"""
    names = [('OLED 状态屏', 'oled_status.py'),
             ('Web 监控', 'monitor_web.py'),
             ('定时任务 crond', 'crond')]
    try:
        r = subprocess.run(['ps'], capture_output=True, timeout=5)
        out = r.stdout.decode(errors='replace')
        return [{'name': n, 'running': a in out} for n, a in names]
    except Exception:
        return [{'name': n, 'running': False} for n, _ in names]


def sampler():
    """后台采样线程：所有耗时探测都在这里做，API 处理零阻塞"""
    last_rx, last_tx = get_net_bytes()
    last_t = time.time()
    h_rx, h_tx, h_t = last_rx, last_tx, last_t     # 分钟级窗口
    last_idle, last_cpu = read_cpu()
    prev_ctrs = read_nft_counters()
    prev_ctr_t = time.time()
    STATE['hist'] = load_hist()
    n = 0
    while True:
        time.sleep(SAMPLER_EVERY)
        try:
            now = time.time()
            rx, tx = get_net_bytes()
            dt = max(now - last_t, 0.01)
            down = (rx - last_rx) / dt if rx >= last_rx else 0
            up = (tx - last_tx) / dt if tx >= last_tx else 0
            down = min(down, LINK_CAP)
            up = min(up, LINK_CAP)
            last_rx, last_tx, last_t = rx, tx, now

            idle, total = read_cpu()
            di, dtot = idle - last_idle, total - last_cpu
            cpu = (dtot - di) * 100 / dtot if dtot > 0 else 0
            last_idle, last_cpu = idle, total

            leases = read_leases()
            ctrs = read_nft_counters()
            dev_rate = {}
            for h, cur in ctrs.items():
                prev = prev_ctrs.get(h)
                if prev:
                    d_up = (cur['up'] - prev['up']) / (now - prev_ctr_t)
                    d_dn = (cur['down'] - prev['down']) / (now - prev_ctr_t)
                    if d_up >= 0 and d_dn >= 0:      # 计数器重建会出现负差，跳过
                        dev_rate[h] = {'up': d_up, 'down': d_dn}
            prev_ctrs, prev_ctr_t = ctrs, now

            with LOCK:
                STATE['trend'].append({'t': int(now), 'up': up, 'down': down})
                del STATE['trend'][:-TREND_MAX]
                STATE['online'] = check_online()
                STATE['clients'] = get_clients()
                STATE['cpu_pct'] = cpu
                STATE['dev_rate'] = dev_rate
                STATE['leases'] = leases

            # 分钟级流量点 + 3 天持久化
            if n % 12 == 11:
                hd = max(now - h_t, 0.01)
                h_up = min((tx - h_tx) / hd if tx >= h_tx else 0, LINK_CAP)
                h_dn = min((rx - h_rx) / hd if rx >= h_rx else 0, LINK_CAP)
                h_rx, h_tx, h_t = rx, tx, now
                with LOCK:
                    STATE['hist'].append({'t': int(now), 'up': h_up, 'down': h_dn})
                    del STATE['hist'][:-HIST_MAX]
            if n % 24 == 0:
                save_hist()

            if n % 6 == 0:                            # 半分钟刷一次足够
                STATE['wan_ip'] = get_wan_ip()
                STATE['gateway'] = get_gateway()
            if n % 60 == 0:                           # 5 分钟刷一次服务状态
                STATE['services'] = get_services()
            n += 1
        except Exception:
            pass


def daily_history():
    """按天流量（估算）：/root/traffic_log 每天最后一行是设备累计快照，
    相邻两天快照差值 = 当天用量。粒度 0.1G，仅供趋势参考。"""
    try:
        files = sorted(f for f in os.listdir(LOG_DIR)
                       if re.fullmatch(r'\d{4}-\d{2}-\d{2}\.txt', f))
    except Exception:
        return []
    days = []
    for fn in files:
        try:
            lines = [l for l in open(os.path.join(LOG_DIR, fn)) if l.strip()]
            if not lines:
                continue
            vals = [float(x) * 1073741824
                    for x in re.findall(r'=(\d+(?:\.\d+)?)G', lines[-1])]
            days.append({'date': fn[:-4], 'cum': sum(vals)})
        except Exception:
            continue
    out = []
    for i, d in enumerate(days):
        prev = days[i - 1]['cum'] if i else 0
        out.append({'date': d['date'], 'total': max(d['cum'] - prev, 0)})
    return out[-30:]


def auth_info():
    """认证日志：SUCCESS 计数 + 最近成功时间 + 最近 5 行（基于文件尾部 200 行）"""
    try:
        with open(AUTH_LOG, errors='replace') as f:
            lines = f.read().splitlines()[-200:]
    except Exception:
        return {'count': 0, 'last': '', 'recent': []}
    succ = [l for l in lines if 'SUCCESS' in l]
    fail = [l for l in lines if 'FAIL' in l]
    last = ''
    if succ:
        m = re.match(r'\[([^\]]+)\]', succ[-1])
        last = m.group(1) if m else succ[-1]
    return {'count': len(succ), 'last': last, 'fail': len(fail), 'recent': lines[-5:]}


def api_status():
    with LOCK:
        trend = list(STATE['trend'])
        online = STATE['online']
        clients = STATE['clients']
        cpu = STATE['cpu_pct']
        dev_rate = dict(STATE['dev_rate'])
        leases = dict(STATE['leases'])
        services = list(STATE['services'])
    devs, _ = load_dev_state()
    total_rx, total_tx = load_total()

    items = []
    for mac, e in devs.items():
        ip = next((k for k, v in leases.items() if v[0].lower() == mac.lower()), '')
        rate = dev_rate.get(ip_to_h(ip), {'up': 0, 'down': 0}) if ip else {'up': 0, 'down': 0}
        items.append({'name': e.get('name') or mac[-5:], 'mac': mac, 'ip': ip,
                      'rx': e.get('rx', 0), 'tx': e.get('tx', 0),
                      'up_bps': rate['up'], 'down_bps': rate['down'],
                      'online': bool(ip)})
    items.sort(key=lambda x: x['rx'] + x['tx'], reverse=True)

    try:
        uptime = int(float(open('/proc/uptime').read().split()[0]))
    except Exception:
        uptime = 0
    try:
        load = open('/proc/loadavg').read().split()[:3]
    except Exception:
        load = ['0', '0', '0']

    last = trend[-1] if trend else {'up': 0, 'down': 0}
    return {
        'time': time.strftime('%H:%M:%S'),
        'uptime_s': uptime,
        'online': online,
        'wan_ip': STATE.get('wan_ip', ''),
        'gateway': STATE.get('gateway', ''),
        'clients': clients,
        'up_bps': last['up'], 'down_bps': last['down'],
        'total_rx': total_rx, 'total_tx': total_tx,
        'temp': get_temp(), 'mem_pct': get_mem_pct(), 'cpu_pct': cpu,
        'load': load, 'services': services,
        'devices': items,
        'trend': trend,
        'auth': auth_info(),
    }


def api_hist(hours):
    with LOCK:
        pts = list(STATE['hist'])
    cutoff = time.time() - hours * 3600
    return [p for p in pts if p['t'] >= cutoff]


def api_log(lines):
    try:
        n = max(1, min(int(lines), 1000))
    except Exception:
        n = 200
    try:
        with open(AUTH_LOG, errors='replace') as f:
            return f.read().splitlines()[-n:]
    except Exception:
        return []


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/status':
            self._send(200, 'application/json', json.dumps(api_status()).encode())
        elif self.path.startswith('/api/history'):     # 必须在 /api/hist 之前判断
            m = re.search(r'[?&]days=(\d+)', self.path)
            days = int(m.group(1)) if m else 30
            h = daily_history()[-days:]
            self._send(200, 'application/json', json.dumps(h).encode())
        elif self.path.startswith('/api/hist'):
            m = re.search(r'[?&]h=(\d+)', self.path)
            hours = int(m.group(1)) if m else 24
            hours = min(max(hours, 1), HIST_DAYS * 24)
            self._send(200, 'application/json', json.dumps(api_hist(hours)).encode())
        elif self.path.startswith('/api/log'):
            m = re.search(r'[?&]lines=(\d+)', self.path)
            self._send(200, 'application/json',
                       json.dumps(api_log(m.group(1) if m else 200)).encode())
        elif self.path.split('?')[0] in ('/', '/index.html'):
            try:
                self._send(200, 'text/html; charset=utf-8', open(HTML_PATH, 'rb').read())
            except Exception:
                self._send(404, 'text/plain; charset=utf-8', 'monitor.html 缺失'.encode())
        else:
            self._send(404, 'text/plain; charset=utf-8', b'not found')

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        # 三重禁缓存：避免浏览器复用旧页面/旧数据（改版后必须即时生效）
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):        # 静默访问日志
        pass


def main():
    threading.Thread(target=sampler, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()


if __name__ == '__main__':
    main()
