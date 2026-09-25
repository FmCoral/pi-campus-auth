#!/usr/bin/env python3
# 只读侦察：分析 eportal 首页，找主动下线接口线索（绝不访问 logout）
import urllib.request, urllib.error, re

def get(url, timeout=8):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, dict(r.headers), r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode('utf-8', 'replace')

base = 'http://10.100.0.12'
s, h, b = get(base + '/eportal/index.jsp')
print(f'index.jsp -> {s}, len={len(b)}, Location={h.get("Location")}')
print('===== full body (first 2000) =====')
print(b[:2000])
print()
print('===== logout / portal API hints =====')
for m in re.finditer(r'[^\n]{0,80}(logout|portal/|下线|注销|success)[^\n]{0,80}', b, re.I):
    print(m.group(0)[:200])
print()
print('===== script srcs =====')
for m in re.finditer(r'<script[^>]+src=["\']([^"\']+)', b):
    print(m.group(1))
