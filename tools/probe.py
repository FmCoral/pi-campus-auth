#!/usr/bin/env python3
# 探测校园网认证链，确认 sso 是否真实响应、encryptor 类型
import urllib.request, urllib.parse, http.cookiejar, ssl, re, json, sys

ctx = ssl._create_unverified_context()
cj = http.cookiejar.CookieJar()

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 不自动跟，手动看 Location

opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(cj),
    urllib.request.HTTPSHandler(context=ctx),
    NoRedirect,
)
opener.addheaders = [('User-Agent', 'Mozilla/5.0')]

def fetch(url, method='GET', data=None):
    req = urllib.request.Request(url, method=method, data=data)
    try:
        resp = opener.open(req, timeout=15)
        return resp.status, dict(resp.headers), resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode('utf-8', 'replace')

def show(name, url, status, headers, body):
    print(f"\n=== {name} ===")
    print(f"URL: {url}")
    print(f"status: {status}")
    loc = headers.get('Location') or headers.get('location')
    if loc:
        print(f"Location: {loc}")
    print(f"len: {len(body)}")
    print(f"head: {body[:250]}")
    return loc

# Step 1: 触发 ePortal redirect（HTTP 劫持）
url1 = "http://1.1.1.1/"
s, h, b = fetch(url1)
show("1. trigger redirect", url1, s, h, b)
m = re.search(r"http://10\.100\.0\.12/eportal/[a-z]+\.jsp\?[^']+", b)
if not m:
    print("\n!! no eportal url found")
    sys.exit(1)
eportal = m.group(0)
print(f"\nextracted eportal: {eportal}")

# Step 2: 访问 ePortal index.jsp → 看 Location
s, h, b = fetch(eportal)
loc = show("2. eportal index.jsp", eportal, s, h, b)
if not loc:
    # 可能 body 里有 login_sso.jsp 链接
    m2 = re.search(r'(login_sso\.jsp\?[^"\'\s<>]+)', b)
    if m2:
        loc = 'http://10.100.0.12/eportal/' + m2.group(1)
        print(f"found login_sso in body: {loc}")
if not loc:
    print("\n!! no location from eportal, stop")
    sys.exit(1)

# Step 3: 跟 Location 链，直到拿到 cas/login
cur = loc
for i in range(8):
    s, h, b = fetch(cur)
    loc = show(f"3.{i} follow", cur, s, h, b)
    if 'cas/login' in cur or 'cas/login' in (b[:500]):
        print("\n*** reached cas/login ***")
        # 提取 sig_request
        msig = re.search(r'sig_request\s*[:=]\s*["\']([^"\']+)["\']', b)
        if msig:
            sig = msig.group(1)
            print(f"sig_request: {sig}")
            parts = sig.split('|')
            print(f"sig parts ({len(parts)}): {[p[:30] for p in parts]}")
            if len(parts) >= 2:
                cims_sig = parts[0]
                app_sig = parts[1]
                print(f"cims_sig: {cims_sig[:60]}")
                print(f"app_sig: {app_sig[:60]}")
        else:
            print("!! no sig_request in cas/login")
            print("body head:", b[:800])
        break
    if not loc:
        print("!! no more location")
        break
    cur = loc

print("\n=== cookies ===")
for c in cj:
    print(f"{c.name}={c.value[:40]}... domain={c.domain} path={c.path}")
