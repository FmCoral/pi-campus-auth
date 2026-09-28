#!/usr/bin/env python3
# Web 监控面板 - 单文件 HTTP 服务（python3 标准库，零依赖）
#
#   页面  http://192.168.1.1:8080/            → monitor.html（每次请求现读，改完即生效）
#   接口  /api/status                          → 总览/设备/系统（2s 轮询）
#         /api/system                          → 系统详情：分核CPU/内存明细/存储/接口/进程/温度曲线
#         /api/hist?h=24|72                    → 分钟级流量历史（3 天环形缓冲）
#         /api/history?days=30                 → 按天流量（traffic_log 差值估算）
#         /api/log?lines=200                   → 认证日志尾部
#
# 数据复用 oled_status.py 的采集函数（同目录 import，单一数据源）。
# 后台线程每 5s 采样；分钟流量点每 60s 一个，持久化 /root/traffic_hist.json
# （默认保留 3 天，每 2 分钟原子落盘一次，重启/关机最多丢 2 分钟）。
# 部署：/root/monitor_web.py + /root/monitor.html，procd 服务 /etc/init.d/monitorweb
# 防火墙：wan 区 input REJECT，校园网侧访问不到；仅 LAN 可看。

import json, os, re, sys, time, threading, subprocess, socket, struct
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from oled_status import (read_leases, load_dev_state, read_nft_counters, h_to_ip,
                         ip_to_h, get_temp, get_mem_pct, read_cpu, get_net_bytes,
                         check_online, get_clients, load_total, LOG_DIR)

PORT = 8080
HTML_PATH = os.path.join(BASE, 'monitor.html')
AUTH_LOG = '/root/auto_auth.log'
HIST_FILE = '/root/traffic_hist.json'
SEEN_FILE = '/root/device_seen.json'   # 设备最后在线时间 {mac_lower: ts}，离线设备展示用
DEST_FILE = '/root/traffic_dest.json'  # 设备流量去向（按天/按服务）{days:{date:{mac:{svc:bytes}}}}
DEST_DAYS = 30
CONNTRACK_FILE = '/proc/net/nf_conntrack'
HIST_DAYS = 3                # 分钟级历史保留天数
HIST_MAX = HIST_DAYS * 1440  # 4320 点
TREND_MAX = 60               # 5 分钟趋势（5s 一个点）
SAMPLER_EVERY = 5            # 采样周期（秒）
LINK_CAP = 125_000_000       # 1Gbps 线速；超过视为计数器异常，钳制（防坏点破坏图表缩放）

# ---- 系统详情页（/api/system）----
CLK_TCK = os.sysconf('SC_CLK_TCK')          # 用户态 HZ，通常 100
PAGE_SZ = os.sysconf('SC_PAGE_SIZE')        # 内存页 4096
TEMP_HIST_MAX = 720                         # 温度缓冲：1 小时（5s 一点，内存，重启清零）
NET_IFACES = ['eth0', 'phy0-ap0', 'br-lan']
SERVICE_ANCHORS = [                         # 关键守护进程（进程名子串）
    ('OLED 状态屏', 'oled_status.py'),
    ('Web 监控', 'monitor_web.py'),
    ('定时任务 crond', 'crond'),
    ('DHCP/DNS dnsmasq', 'dnsmasq'),
    ('Wi-Fi hostapd', 'hostapd'),
    ('网络管理 netifd', 'netifd'),
]

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
    'services': [],         # [{'name','running','uptime'}]
    'temp_hist': [],        # [{'t','temp'}] 最近 1 小时
    'procs': [],            # [{'pid','name','cpu','rss'}] Top 进程
    'cpu_detail': None,     # {'agg': 分类占比, 'cores': [4]}
    'stations': {},         # 在线 station {mac_lower: {'conn':秒,'txr':协商Mbps,'rxr':Mbps}}
    'seen': {},             # {mac_lower: 最后在线 ts}（启动时从 SEEN_FILE 载入）
    'dns_ip': {},           # {公网IP: (域名, ts)}，DNS 嗅探线程维护，conntrack 归属用
    'dest': {'days': {}},   # 流量去向累计，启动时从 DEST_FILE 载入
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


def load_seen():
    """设备在线记录 {mac_lower: {'s':最后在线ts, 't':累计在线秒}}。
    旧版文件是 {mac: ts}，载入时自动迁移，累计时长从 0 开始。"""
    try:
        data = json.load(open(SEEN_FILE))
    except Exception:
        return {}
    out = {}
    for mac, v in data.items():
        if isinstance(v, (int, float)):
            out[mac] = {'s': int(v), 't': 0}
        elif isinstance(v, dict):
            out[mac] = {'s': int(v.get('s', 0)), 't': int(v.get('t', 0))}
    return out


def save_seen():
    tmp = SEEN_FILE + '.tmp'
    with LOCK:
        data = dict(STATE['seen'])
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, SEEN_FILE)
    except Exception:
        pass


# ---------- 流量去向：域名→服务商归属 ----------
# 顺序即优先级（专属服务在前，通用兜底在后）。含点的 token 按域名后缀匹配，
# 不含点的按子串匹配。无法归属的统一进「其他」，用 DoH 的应用天然无法识别。
SERVICE_MAP = [
    ('微信', ['weixin.qq.com', 'weixin.', 'wechat', 'tenpay', 'weixinbridge']),
    ('QQ', ['qq.com', 'qqmail', 'qpic.cn', 'qlogo.cn', 'gtimg.cn', 'gtimg.com',
            'idqqimg', 'qqurl']),
    ('抖音/头条', ['douyin', 'iesdouyin', 'bytedance', 'pstatp.com', 'bytecdn',
               'byteimg', 'toutiao', 'snssdk', 'feiliao', 'volces']),
    ('快手', ['kuaishou', 'yximgs', 'gifshow']),
    ('哔哩哔哩', ['bilibili', 'bilivideo', 'hdslb', 'biliapi', 'biligame']),
    ('淘宝/阿里', ['taobao', 'tmall', 'alicdn', 'mmstat', 'alibaba', 'aliyun',
               'tb.cn', 'etaoshi', 'alipay', 'amap']),
    ('拼多多', ['pinduoduo', 'yangkeduo', 'pddpic']),
    ('京东', ['jd.com', 'jd.hk', 'jdcloud', '360buyimg']),
    ('美团/点评', ['meituan', 'dianping', 'meituan.net', 'dpfile']),
    ('百度', ['baidu', 'bdstatic', 'bdimg', 'bcebos', 'baidubcs']),
    ('网易', ['163.com', '126.com', 'netease', '126.net', '163.net', 'ydstatic']),
    ('小米', ['mi.com', 'xiaomi', 'miui', 'mi-img', 'mi-fds', 'duokan']),
    ('华为', ['hicloud', 'huawei', 'dbankcdn']),
    ('苹果', ['apple.com', 'icloud', 'mzstatic', 'icloud-content']),
    ('微软', ['microsoft', 'windows', 'office', 'bing.com', 'live.com',
           'msftconnect']),
    ('Google', ['google', 'gstatic', 'googleapis', 'googleusercontent',
             'gvt1', 'gvt2', 'gvt3']),
    ('腾讯', ['tencent', 'myqcloud', 'qcloud', 'wechat']),
]
LAN_NAME = '局域网'
OTHER_NAME = '其他'


def classify_svc(domain):
    """域名 → 服务商名"""
    d = domain.strip('.').lower()
    for name, toks in SERVICE_MAP:
        for t in toks:
            if '.' in t:
                if d == t or d.endswith('.' + t):
                    return name
            elif t in d:
                return name
    return OTHER_NAME


def is_lan(ip):
    return (ip.startswith('192.168.') or ip.startswith('10.') or
            ip.startswith('169.254.') or
            any(ip.startswith('172.%d.' % i) for i in range(16, 32)))


def load_dest():
    """启动恢复去向累计，丢弃超期日期，兼容损坏文件"""
    try:
        d = json.load(open(DEST_FILE))
    except Exception:
        return {'days': {}}
    if not isinstance(d, dict) or 'days' not in d:
        return {'days': {}}
    cutoff = (date.today() - timedelta(days=DEST_DAYS - 1)).isoformat()
    d['days'] = {k: v for k, v in d['days'].items() if k >= cutoff}
    return d


def save_dest():
    tmp = DEST_FILE + '.tmp'
    cutoff = (date.today() - timedelta(days=DEST_DAYS - 1)).isoformat()
    with LOCK:
        days = {k: v for k, v in STATE['dest']['days'].items() if k >= cutoff}
        data = {'days': days}
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, DEST_FILE)
    except Exception:
        pass


# ---------- DNS 原始套接字嗅探（零系统改动）----------
def dns_name(buf, off):
    """解析（可能压缩指针的）DNS 名 → (小写域名, 记录结束偏移)"""
    labels, end, jumped = [], off, False
    while True:
        n = buf[off]
        if n & 0xc0 == 0xc0:
            ptr = ((n & 0x3f) << 8) | buf[off + 1]
            if not jumped:
                end = off + 2
            off, jumped = ptr, True
        elif n == 0:
            if not jumped:
                end = off + 1
            break
        else:
            labels.append(buf[off + 1:off + 1 + n].decode(errors='replace').lower())
            off += 1 + n
            if not jumped:
                end = off
    return '.'.join(labels), end


def parse_dns_reply(payload):
    """从 DNS 响应中提取 A 记录。把报文内全部名字（问题名、CNAME 链、记录名）
    合并归类——CDN 响应里 A 记录名是基础设施域名，原始 App 域名只出现在
    CNAME 链首/问题里。"""
    if len(payload) < 12 or not (payload[2] & 0x80):
        return
    qd = int.from_bytes(payload[4:6], 'big')
    an = int.from_bytes(payload[6:8], 'big')
    off = 12
    now = time.time()
    names, ips = [], []
    try:
        for _ in range(qd):
            nm, off = dns_name(payload, off)
            off += 4
            names.append(nm)
        for _ in range(an):
            rname, off = dns_name(payload, off)
            typ = int.from_bytes(payload[off:off + 2], 'big')
            rdl = int.from_bytes(payload[off + 8:off + 10], 'big')
            off += 10
            names.append(rname)
            if typ == 1 and rdl == 4 and off + 4 <= len(payload):
                ips.append('.'.join(str(b) for b in payload[off:off + 4]))
            off += rdl
    except Exception:
        return
    # 在全部候选名里挑能明确归属的；都认不出时取首个名字
    chosen = OTHER_NAME
    for nm in names:
        svc = classify_svc(nm)
        if svc != OTHER_NAME:
            chosen = svc
            break
    if not ips:
        return
    with LOCK:
        for ip in ips:
            STATE['dns_ip'][ip] = (chosen, now)


# BPF：内核侧只放行 UDP 53 端口报文（raw IP 头偏移），避免 QUIC 等全量 UDP 上送
_DNS_BPF = [
    (0x30, 0, 0, 0),      # ldb [0]  version/ihl
    (0x54, 0, 0, 0x0f),   # and #0xf
    (0x64, 0, 0, 2),      # lsh #2   ihl*4
    (0x07, 0, 0, 0),      # tax
    (0x30, 0, 0, 9),      # ldb [9]  protocol
    (0x15, 0, 5, 17),     # jeq #17 udp
    (0x48, 0, 0, 0),      # ldh [x+0] src port
    (0x15, 2, 0, 53),     # jeq 53 accept
    (0x48, 0, 0, 2),      # ldh [x+2] dst port
    (0x15, 0, 1, 53),     # jeq 53 accept / reject
    (0x06, 0, 0, 0xffff), # ret
    (0x06, 0, 0, 0),
]


_BPF_HOLD = []          # 保活 BPF 指令缓冲（内核 setsockopt 后仍需内存存在）
def attach_port_filter(sock_obj):
    # 用 ctypes 创建内核可访问的稳定缓冲，挂 SO_ATTACH_FILTER 让内核只放 53 端口。
    # 注意：本环境 socket 内置对象没有 __dict__，不能在 sock 上挂属性保活。
    import ctypes
    prog = b''.join(struct.pack('HBBI', *ins) for ins in _DNS_BPF)
    arr = (ctypes.c_char * len(prog)).from_buffer_copy(prog)
    fprog = struct.pack('HQ', len(_DNS_BPF), ctypes.addressof(arr))
    sock_obj.setsockopt(socket.SOL_SOCKET, 26, fprog)   # SO_ATTACH_FILTER
    _BPF_HOLD.append(arr)


def dns_sniffer():
    """原始套接字收 DNS 响应（含本机经 dnsmasq 发出的应答），维护 IP→域名"""
    try:
        raw = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_UDP)
        attach_port_filter(raw)
    except Exception:
        return
    last_purge = 0.0
    while True:
        try:
            pkt = raw.recv(65535)
            if len(pkt) < 28:
                continue
            ihl = (pkt[0] & 0xf) * 4
            udl = int.from_bytes(pkt[ihl + 4:ihl + 6], 'big')
            payload = pkt[ihl + 8:ihl + 8 + max(0, udl - 8)]
            if payload:
                parse_dns_reply(payload)
            now = time.time()
            if now - last_purge > 300:
                last_purge = now
                with LOCK:
                    STATE['dns_ip'] = {
                        k: v for k, v in STATE['dns_ip'].items()
                        if now - v[1] < 21600}
        except Exception:
            time.sleep(1)


# ---------- conntrack 流量归属采样 ----------
_CT_TUPLE_RE = re.compile(
    r'src=(\S+) dst=(\S+) sport=(\d+) dport=(\d+) packets=\d+ bytes=(\d+)')
_CT_PREV = {}          # 连接键 → (orig_bytes, reply_bytes)，做增量


def sample_dest(leases):
    """每 10 秒：解析 conntrack 增量，按 IP→域名表归属到服务桶并按天累计。
    原始组元组字节 = 设备上行，应答组元组字节 = 设备下行。"""
    try:
        lines = open(CONNTRACK_FILE).read().splitlines()
    except Exception:
        return
    now = time.time()
    with LOCK:
        dns_ip = dict(STATE['dns_ip'])
    adds, seen_keys = {}, set()
    for line in lines:
        t = _CT_TUPLE_RE.findall(line)
        if len(t) < 2:
            continue
        o, r = t[0], t[1]
        devip = o[0]
        if devip not in leases:
            continue                 # 路由器自身（cloudflared 等）及非 DHCP 来源：不进设备统计
        if is_lan(o[1]):
            continue                    # 目的是路由器/内网：跳过
        key = (line.split()[2] if len(line.split()) > 2 else '?',
               o[0], o[2], o[1], o[3], r[0], r[2], r[1], r[3])
        seen_keys.add(key)
        up_b, dn_b = int(o[4]), int(r[4])
        prev = _CT_PREV.get(key)
        du = max(0, up_b - prev[0]) if prev else 0
        dd = max(0, dn_b - prev[1]) if prev else 0
        if du + dd == 0:
            continue
        mac = (leases.get(devip) or [''])[0]
        mk = mac.lower() if mac else devip
        rec = dns_ip.get(r[0])
        svc = rec[0] if rec else OTHER_NAME
        b = adds.setdefault(mk, {})
        b[svc] = b.get(svc, 0) + du + dd

    for k in list(_CT_PREV):            # 关闭的连接剔除
        if k not in seen_keys:
            _CT_PREV.pop(k, None)
    for line in lines:                  # 更新本轮字节基线
        t = _CT_TUPLE_RE.findall(line)
        if len(t) < 2:
            continue
        o, r = t[0], t[1]
        key = (line.split()[2] if len(line.split()) > 2 else '?',
               o[0], o[2], o[1], o[3], r[0], r[2], r[1], r[3])
        _CT_PREV[key] = (int(o[4]), int(r[4]))

    if adds:
        ds = time.strftime('%Y-%m-%d')
        with LOCK:
            day = STATE['dest']['days'].setdefault(ds, {})
            for mk, buckets in adds.items():
                row = day.setdefault(mk, {})
                for svc, b in buckets.items():
                    row[svc] = row.get(svc, 0) + b


def device_dest_info(mac, days):
    """聚合某设备近 days 天的服务桶 → {total, services:[{name,bytes}]}"""
    days = max(1, min(int(days), DEST_DAYS))
    keys = [(date.today() - timedelta(i)).isoformat() for i in range(days)]
    agg = {}
    with LOCK:
        for k in keys:
            row = STATE['dest']['days'].get(k, {}).get(mac.lower(), {})
            for svc, b in row.items():
                agg[svc] = agg.get(svc, 0) + b
    services = sorted(({'name': k, 'bytes': v} for k, v in agg.items()),
                      key=lambda x: -x['bytes'])
    return {'total': sum(agg.values()), 'services': services}


def read_stations():
    """iw station dump → {mac_lower: {'conn':当前连接秒数}}"""
    out = {}
    try:
        r = subprocess.run(['iw', 'dev', 'phy0-ap0', 'station', 'dump'],
                           capture_output=True, timeout=5)
        txt = r.stdout.decode(errors='replace')
    except Exception:
        return out
    for block in txt.split('Station '):
        m = re.match(r'([0-9a-fA-F:]{17})', block)
        if not m:
            continue
        mc = re.search(r'connected time:\s*(\d+)\s*seconds', block)
        out[m.group(1).lower()] = {'conn': int(mc.group(1)) if mc else 0}
    return out


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


def device_info():
    """设备/系统固定信息（启动时取一次）"""
    info = {}
    try:
        rel = {}
        for l in open('/etc/openwrt_release'):
            if '=' in l:
                k, v = l.strip().split('=', 1)
                rel[k] = v.strip("'")
        info['distro'] = rel.get('DISTRIB_DESCRIPTION', '')
        info['revision'] = rel.get('DISTRIB_REVISION', '')
        info['target'] = rel.get('DISTRIB_TARGET', '')
        info['arch'] = rel.get('DISTRIB_ARCH', '')
    except Exception:
        pass
    info['kernel'] = os.uname().release
    info['hostname'] = os.uname().nodename
    model = ''
    for p in ('/tmp/sysinfo/model', '/proc/device-tree/model'):
        try:
            model = open(p).read().strip().rstrip('\x00')
            if model:
                break
        except Exception:
            pass
    info['model'] = model or os.uname().machine
    try:
        for l in open('/proc/stat'):
            if l.startswith('btime'):
                info['boot'] = int(l.split()[1])
    except Exception:
        pass
    return info


DEV_INFO = device_info()


def read_cpu_stat():
    """/proc/stat：{'agg': [jiffies...], 'cores': [[...]]}"""
    lines = open('/proc/stat').read().splitlines()
    agg = list(map(int, lines[0].split()[1:]))
    cores = []
    for l in lines[1:]:
        if l.startswith('cpu'):
            cores.append(list(map(int, l.split()[1:])))
        else:
            break
    return {'agg': agg, 'cores': cores}


def cpu_breakdown(prev, cur):
    """两次快照差值 → {'agg': 分类占比, 'cores': [各核繁忙%]}"""
    def ratio(p, c):
        d = [max(c[i] - p[i], 0) for i in range(min(len(p), len(c)))]
        tot = sum(d) or 1
        return {'user': d[0] * 100 / tot, 'nice': d[1] * 100 / tot,
                'sys': d[2] * 100 / tot, 'iowait': (d[4] if len(d) > 4 else 0) * 100 / tot,
                'irq': ((d[5] if len(d) > 5 else 0) + (d[6] if len(d) > 6 else 0)) * 100 / tot,
                'busy': 100 - (d[3] + (d[4] if len(d) > 4 else 0)) * 100 / tot}
    return {'agg': ratio(prev['agg'], cur['agg']),
            'cores': [ratio(p, c)['busy']
                      for p, c in zip(prev['cores'], cur['cores'])]}


def mem_detail():
    """/proc/meminfo 明细（字节）"""
    m = {}
    for l in open('/proc/meminfo'):
        k, _, v = l.partition(':')
        try:
            m[k] = int(v.split()[0]) * 1024
        except Exception:
            pass
    tot = m.get('MemTotal', 1)
    return {'total': m.get('MemTotal', 0), 'avail': m.get('MemAvailable', 0),
            'used_real': tot - m.get('MemAvailable', 0),
            'buffers': m.get('Buffers', 0), 'cached': m.get('Cached', 0),
            'free': m.get('MemFree', 0)}


def storage_info():
    """df：/（ext4）、/boot、/tmp(tmpfs) 占用"""
    out = []
    try:
        r = subprocess.run(['df'], capture_output=True, timeout=5, text=True)
        for l in r.stdout.splitlines()[1:]:
            p = l.split()
            if len(p) >= 6 and p[5] in ('/', '/boot', '/tmp'):
                out.append({'fs': p[0], 'mount': p[5], 'total': int(p[1]) * 1024,
                            'used': int(p[2]) * 1024, 'avail': int(p[3]) * 1024,
                            'pct': int(p[4].rstrip('%'))})
    except Exception:
        pass
    order = {'/': 0, '/boot': 1, '/tmp': 2}
    out.sort(key=lambda x: order.get(x['mount'], 9))
    return out


def net_ifaces():
    """接口：MAC/IP/累计收发（/sys/class/net + ip addr）"""
    out = []
    for name in NET_IFACES:
        d = '/sys/class/net/' + name
        if not os.path.exists(d):
            continue
        e = {'name': name,
             'op': open(d + '/operstate').read().strip() == 'up',
             'mac': open(d + '/address').read().strip(),
             'rx': int(open(d + '/statistics/rx_bytes').read()),
             'tx': int(open(d + '/statistics/tx_bytes').read()), 'ip': ''}
        try:
            r = subprocess.run(['ip', '-4', 'addr', 'show', 'dev', name],
                               capture_output=True, timeout=5, text=True)
            mm = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', r.stdout)
            if mm:
                e['ip'] = mm.group(1)
        except Exception:
            pass
        out.append(e)
    return out


def proc_snapshot():
    """所有进程 {pid: {'ticks': utime+stime, 'rss': 页数, 'comm': 名}}"""
    snap = {}
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            data = open('/proc/%s/stat' % name).read()
            rp = data.rfind(')')
            f = data[rp + 2:].split()      # f[0]=field3 → utime f[11], stime f[12], rss f[21]
            comm = data[data.find('(') + 1:rp]
            # stat 的 comm 限 15 字符，python 脚本只显示 "python3"；用 cmdline 补全脚本名
            if comm in ('python3', 'python', 'python2'):
                try:
                    cl = open('/proc/%s/cmdline' % name).read().replace('\x00', ' ').strip().split()
                    if len(cl) > 1:
                        comm = comm + ' ' + os.path.basename(cl[1])
                except Exception:
                    pass
            snap[int(name)] = {'ticks': int(f[11]) + int(f[12]),
                               'rss': int(f[21]), 'comm': comm}
        except Exception:
            continue
    return snap


def proc_top(prev, cur):
    """两次快照 → Top 进程（CPU% 相对单核 + RSS 字节），取 CPU/RSS 各自前列的并集"""
    rows = []
    for pid, c in cur.items():
        p = prev.get(pid)
        if not p:
            continue
        dticks = c['ticks'] - p['ticks']
        if dticks < 0:
            continue
        rows.append({'pid': pid, 'name': c['comm'],
                     'cpu': dticks * 100 / (SAMPLER_EVERY * CLK_TCK),
                     'rss': c['rss'] * PAGE_SZ})
    top_cpu = sorted(rows, key=lambda x: x['cpu'], reverse=True)[:6]
    top_mem = sorted(rows, key=lambda x: x['rss'], reverse=True)[:6]
    union = {p['pid']: p for p in top_cpu + top_mem}
    return sorted(union.values(), key=lambda x: x['cpu'], reverse=True)[:10]


def get_services():
    """关键服务：运行状态 + 已运行时长（读 /proc/pid/stat starttime）"""
    try:
        ps_out = subprocess.run(['ps'], capture_output=True, timeout=5, text=True).stdout
    except Exception:
        ps_out = ''
    try:
        up = float(open('/proc/uptime').read().split()[0])
    except Exception:
        up = 0
    res = []
    for name, anchor in SERVICE_ANCHORS:
        pid = None
        for l in ps_out.splitlines():
            if anchor in l:
                pid = l.split()[0]
                break
        svc_up = None
        if pid:
            try:
                data = open('/proc/%s/stat' % pid).read()
                f = data[data.rfind(')') + 2:].split()   # starttime=field22 → f[19]
                svc_up = max(up - int(f[19]) / CLK_TCK, 0)
            except Exception:
                pass
        res.append({'name': name, 'running': bool(pid), 'uptime': svc_up})
    return res


def api_system():
    with LOCK:
        thist = list(STATE['temp_hist'])
        procs = list(STATE['procs'])
        cpu_d = STATE['cpu_detail']
    try:
        uptime = int(float(open('/proc/uptime').read().split()[0]))
    except Exception:
        uptime = 0
    return {'device': DEV_INFO, 'cpu_detail': cpu_d, 'mem': mem_detail(),
            'storage': storage_info(), 'ifaces': net_ifaces(),
            'procs': procs, 'temp_hist': thist,
            'services': get_services(), 'uptime_s': uptime,
            'cpu_pct': STATE.get('cpu_pct', 0), 'temp': get_temp(),
            'mem_pct': get_mem_pct(), 'load': open('/proc/loadavg').read().split()[:3]}


def sampler():
    """后台采样线程：所有耗时探测都在这里做，API 处理零阻塞"""
    last_rx, last_tx = get_net_bytes()
    last_t = time.time()
    h_rx, h_tx, h_t = last_rx, last_tx, last_t     # 分钟级窗口
    prev_cpu_s = read_cpu_stat()                   # CPU 全字段（分核/分类）
    prev_proc = proc_snapshot()                    # 进程 tick 快照
    prev_ctrs = read_nft_counters()
    prev_ctr_t = time.time()
    STATE['hist'] = load_hist()
    with LOCK:
        STATE['seen'] = load_seen()
        STATE['dest'] = load_dest()
    prev_online = set()
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

            cur_cpu_s = read_cpu_stat()
            cpu_d = cpu_breakdown(prev_cpu_s, cur_cpu_s)
            cpu = cpu_d['agg']['busy']
            prev_cpu_s = cur_cpu_s

            cur_proc = proc_snapshot()             # Top 进程（CPU% + RSS）
            top_procs = proc_top(prev_proc, cur_proc)
            prev_proc = cur_proc

            leases = read_leases()
            ctrs = read_nft_counters()
            stations = read_stations()
            gone = prev_online - set(stations)    # 本轮消失的设备 = 刚离线
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
                STATE['cpu_detail'] = cpu_d
                STATE['procs'] = top_procs
                STATE['temp_hist'].append({'t': int(now), 'temp': get_temp()})
                del STATE['temp_hist'][:-TEMP_HIST_MAX]
                STATE['dev_rate'] = dev_rate
                STATE['leases'] = leases
                STATE['stations'] = stations
                for mac in stations:              # 在线设备：刷新最后在线 + 累加累计在线
                    rec = STATE['seen'].get(mac)
                    if not rec:
                        rec = {'s': int(now), 't': 0}
                    rec['s'] = int(now)
                    rec['t'] += SAMPLER_EVERY
                    STATE['seen'][mac] = rec

            if gone:
                save_seen()                        # 设备刚离线：立即固化
            prev_online = set(stations)
            if n % 12 == 0:
                save_seen()                        # 每 60 秒落盘（断电最多丢 1 分钟累计）

            if n % 2 == 0:                         # 每 10 秒做流量归属累计
                sample_dest(leases)
                if n % 12 == 0:
                    save_dest()

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


# ---- 流量统计页（/api/traffic）：基于 traffic_log 15 分钟快照聚合 ----
SNAP_RE = re.compile(r'^\[(\d{2}):(\d{2})\]\s+(.*)$')
SNAP_KV_RE = re.compile(r'(\S+)=([\d.]+)G')


def parse_snaps(date_str):
    """一天的快照行 → [(hh, mm, {设备名: bytes}), ...]"""
    rows = []
    try:
        for line in open(os.path.join(LOG_DIR, date_str + '.txt'), errors='replace'):
            m = SNAP_RE.match(line.strip())
            if not m:
                continue
            devs = {name: float(g) * 1073741824
                    for name, g in SNAP_KV_RE.findall(m.group(3))}
            if devs:
                rows.append((int(m.group(1)), int(m.group(2)), devs))
    except Exception:
        pass
    return rows


def integrate_hist(pts, t0, t1):
    """分钟速率点按相邻时间间隔积分，返回 (down_bytes, up_bytes)"""
    sel = [p for p in pts if t0 <= p['t'] <= t1]
    d = u = 0.0
    for i, p in enumerate(sel):
        prev_t = t0 if i == 0 else sel[i - 1]['t']
        dt = max(min(p['t'] - prev_t, 300), 1)
        d += p['down'] * dt
        u += p['up'] * dt
    return d, u


def traffic_info():
    now = int(time.time())
    lt = time.localtime(now)
    day0 = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))

    today_d = date(lt.tm_year, lt.tm_mon, lt.tm_mday)
    today = today_d.isoformat()
    yesterday = (today_d - timedelta(1)).isoformat()
    prevday = (today_d - timedelta(2)).isoformat()
    last7 = [(today_d - timedelta(i)).isoformat() for i in range(6, -1, -1)]
    prev7 = [(today_d - timedelta(i)).isoformat() for i in range(13, 6, -1)]
    last30 = [(today_d - timedelta(i)).isoformat() for i in range(29, -1, -1)]
    month_prefix = today[:7]

    # 解析最近 30 天快照（每天约 96 行，量很小）
    snaps = {}
    try:
        for fn in os.listdir(LOG_DIR):
            ds = fn[:-4]
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}\.txt', fn) and ds >= last30[0]:
                rows = parse_snaps(ds)
                if rows:
                    snaps[ds] = rows
    except Exception:
        pass

    dates = sorted(snaps)
    last_snap = {ds: snaps[ds][-1][2] for ds in dates}
    last_total = {ds: sum(d.values()) for ds, d in last_snap.items()}

    # 每日总量 & 每日每设备用量（相邻存在文件快照差值，负差钳 0）
    day_total, day_dev = {}, {}
    for i, ds in enumerate(dates):
        prev = last_snap[dates[i - 1]] if i else {}
        day_total[ds] = max(last_total[ds] - (sum(prev.values()) if i else 0), 0)
        day_dev[ds] = {n: max(v - prev.get(n, 0), 0) for n, v in last_snap[ds].items()}

    def sum_dates(dlist):
        return sum(day_total.get(d, 0) for d in dlist)

    today_total = day_total.get(today, 0)
    month_total = sum(v for d, v in day_total.items() if d.startswith(month_prefix))

    # 昨日"同一时刻"累计（截至当前时分），用于今日环比
    nowhm = lt.tm_hour * 60 + lt.tm_min
    y_same = None
    for hh, mm, d in snaps.get(yesterday, []):
        if hh * 60 + mm <= nowhm:
            y_same = sum(d.values())
    y_used = max(y_same - last_total.get(prevday, 0), 0) if y_same is not None else None
    delta_pct = round((today_total - y_used) / y_used * 100, 1) if y_used else None

    # 小时桶（今日 / 昨日；跨天首段增量归属当天第一行的小时）
    def hour_buckets(ds, base_d):
        hrs = [0.0] * 24
        prev_v = last_total.get(base_d, 0)
        for hh, mm, d in snaps.get(ds, []):
            v = sum(d.values())
            hrs[hh] += max(v - prev_v, 0)
            prev_v = v
        return hrs

    hours_today = hour_buckets(today, yesterday)
    hours_yday = hour_buckets(yesterday, prevday)
    busy = sorted(({'h': h, 'v': v} for h, v in enumerate(hours_today) if v > 0),
                  key=lambda x: -x['v'])[:3]

    # 分钟速率点：今日上下行分解 + 24h 峰值/均值
    with LOCK:
        pts = list(STATE['hist'])
    today_dn, today_up = integrate_hist(pts, day0, now)
    d24, u24 = integrate_hist(pts, now - 86400, now)
    peak_rate = peak_t = 0
    for p in pts:
        if p['t'] >= now - 86400 and p['down'] > peak_rate:
            peak_rate, peak_t = p['down'], p['t']
    tot24 = d24 + u24
    avg24 = tot24 / 86400

    # 设备窗口聚合（今日 / 7 天 / 30 天）
    def dev_window(dlist):
        out = {}
        for ds in dlist:
            for n, v in day_dev.get(ds, {}).items():
                out[n] = out.get(n, 0) + v
        return out

    w_today, w_7, w_30 = dev_window([today]), dev_window(last7), dev_window(last30)
    names = sorted(set(w_today) | set(w_7) | set(w_30),
                   key=lambda n: (-w_today.get(n, 0), n))
    devices = [{'name': n, 'today': w_today.get(n, 0), 'd7': w_7.get(n, 0),
                'd30': w_30.get(n, 0),
                'pct': round(w_today.get(n, 0) / today_total * 100, 1)
                       if today_total else 0}
               for n in names]
    top_dev = devices[0]['name'] if devices and devices[0]['today'] else ''
    top_pct = devices[0]['pct'] if top_dev else 0

    active_hours = sum(1 for v in hours_today if v > today_total / 24)
    s7, p7 = sum_dates(last7), sum_dates(prev7)
    week_delta = round((s7 - p7) / p7 * 100, 1) if p7 else None

    return {
        'today': {'total': today_total, 'down': round(today_dn), 'up': round(today_up),
                  'delta_pct': delta_pct},
        'month': {'total': month_total, 'days': lt.tm_mday},
        'peak': {'rate': peak_rate, 't': peak_t},
        'avg24': round(avg24),
        'hours_today': [round(v) for v in hours_today],
        'hours_yday': [round(v) for v in hours_yday],
        'busy': busy,
        'struct': {'down_pct': round(d24 / tot24 * 100, 1) if tot24 else 0,
                   'up_pct': round(u24 / tot24 * 100, 1) if tot24 else 0,
                   'peak_avg': round(peak_rate / avg24, 1) if avg24 else 0,
                   'active_hours': active_hours,
                   'top_dev': top_dev, 'top_pct': top_pct},
        'daily': daily_history(),
        'hist_span': {'age_h': round((now - pts[0]['t']) / 3600, 1) if pts else 0},
        'day_avg7': round(s7 / 7), 'week_delta': week_delta,
        'devices': devices,
    }


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
        stations = dict(STATE['stations'])
        seen = dict(STATE['seen'])
    devs, _ = load_dev_state()
    total_rx, total_tx = load_total()

    items = []
    for mac, e in devs.items():
        ip = next((k for k, v in leases.items() if v[0].lower() == mac.lower()), '')
        rate = dev_rate.get(ip_to_h(ip), {'up': 0, 'down': 0}) if ip else {'up': 0, 'down': 0}
        st = stations.get(mac.lower())
        rec = seen.get(mac.lower()) or {}
        items.append({'name': e.get('name') or mac[-5:], 'mac': mac, 'ip': ip,
                      'rx': e.get('rx', 0), 'tx': e.get('tx', 0),
                      'up_bps': rate['up'], 'down_bps': rate['down'],
                      'online': st is not None,
                      'conn_s': st['conn'] if st else 0,
                      'last_seen': rec.get('s', 0),
                      'total_online': rec.get('t', 0)})
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
        elif self.path.startswith('/api/traffic'):
            self._send(200, 'application/json', json.dumps(traffic_info()).encode())
        elif self.path.startswith('/api/device_dest'):
            q = parse_qs(urlparse(self.path).query)
            mac = q.get('mac', [''])[0]
            days = q.get('days', ['1'])[0]
            try:
                days_i = int(days)
            except Exception:
                days_i = 1
            self._send(200, 'application/json',
                       json.dumps(device_dest_info(mac, days_i)).encode())
        elif self.path.startswith('/api/system'):
            self._send(200, 'application/json', json.dumps(api_system()).encode())
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
    threading.Thread(target=dns_sniffer, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()


if __name__ == '__main__':
    main()
