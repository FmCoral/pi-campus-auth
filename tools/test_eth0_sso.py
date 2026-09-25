#!/usr/bin/env python3
# 关键验证：纯 eth0（无 coral）能否在"未认证状态"下访问 sso
# 自动链：基线测量 → eportal 下线 → 离线测 sso（含证书指纹对比）→ 立即自动重认证
# 全程约 20~30 秒，输出同时保存 /tmp/test_eth0_sso.log
import socket, ssl, time, sys, hashlib, subprocess
import urllib.request, urllib.error
sys.path.insert(0, '/root')
import auto_auth

OUT = open('/tmp/test_eth0_sso.log', 'w')
def p(*a):
    line = ' '.join(str(x) for x in a)
    print(line, flush=True)
    OUT.write(line + '\n'); OUT.flush()

def ping_ok():
    r = subprocess.run(['ping', '-c', '1', '-W', '2', '8.8.8.8'],
                       capture_output=True, timeout=6)
    return r.returncode == 0

def sso_probe(label):
    """TLS 探测：握手 + 证书指纹（识别 MITM 假证书）+ HTTP 响应头"""
    try:
        raw = socket.create_connection(('sso.hunau.edu.cn', 443), timeout=8)
        ctx = ssl._create_unverified_context()
        ss = ctx.wrap_socket(raw, server_hostname='sso.hunau.edu.cn')
        der = ss.getpeercert(binary_form=True)
        fp = hashlib.sha256(der).hexdigest()[:16]
        ss.sendall(b'GET /cas/login HTTP/1.0\r\nHost: sso.hunau.edu.cn\r\nConnection: close\r\n\r\n')
        head = ss.recv(120)
        ss.close()
        p(f'{label} TLS_OK certfp={fp} resp={head[:80]!r}')
        return fp
    except Exception as e:
        p(f'{label} FAIL {type(e).__name__}: {e}')
        return None

def http_get(url, timeout=8):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read().decode('utf-8', 'replace')[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')[:300]

# ==== 1. 在线基线 ====
p('==== STEP1 baseline (authenticated) ====')
p('ping online:', ping_ok())
fp_online = sso_probe('ONLINE :')

# ==== 2. eportal 主动下线（明文 wlanuserip）====
p('==== STEP2 eportal logout ====')
s, b = http_get('http://10.100.0.12/eportal/portal/logout?wlanuserip=10.101.0.16')
p('logout status:', s, 'body:', b)

time.sleep(3)
p('ping after logout:', ping_ok())

# ==== 3. 离线状态测 sso（核心）====
p('==== STEP3 sso probe while UNAUTHENTICATED ====')
fp_offline = sso_probe('OFFLINE:')

# ==== 4. 立即自动重认证（无论结果，尽快恢复网络）====
p('==== STEP4 auto_auth.authenticate() ====')
ok, msg = auto_auth.authenticate()
p('AUTH RESULT:', ok, msg)
time.sleep(2)
p('ping after auth:', ping_ok())

# ==== 5. 结论 ====
p('==== VERDICT ====')
if fp_offline and fp_online and fp_offline == fp_online:
    p('SSO_VIA_ETH0_OK: 未认证状态可经 eth0 直连 sso，证书一致，无需 coral/sta')
elif fp_offline:
    p('SSO_VIA_ETH0_TLS_OK_BUT_CERT_DIFFERS (注意 MITM)')
else:
    p('SSO_VIA_ETH0_BLOCKED: 未认证状态 sso 不可达，需要保底方案')
OUT.close()
