#!/usr/bin/env python3
# 完整跑认证链到 othertype，看 encryptor 类型
import urllib.request, urllib.parse, http.cookiejar, ssl, re, json

ctx = ssl._create_unverified_context()
cj = http.cookiejar.CookieJar()

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(cj),
    urllib.request.HTTPSHandler(context=ctx),
    NoRedirect,
)
opener.addheaders = [('User-Agent', 'Mozilla/5.0')]

def fetch(url, method='GET', data=None, headers=None):
    h = {'User-Agent':'Mozilla/5.0'}
    if headers: h.update(headers)
    req = urllib.request.Request(url, method=method, data=data, headers=h)
    try:
        resp = opener.open(req, timeout=20)
        return resp.status, dict(resp.headers), resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode('utf-8', 'replace')

USERNAME = "你的学号"

# Step 1: eportal redirect
s, h, b = fetch("http://1.1.1.1/")
m = re.search(r"http://10\.100\.0\.12/eportal/[a-z]+\.jsp\?[^']+", b)
eportal = m.group(0)
print(f"[1] eportal: {eportal[:80]}...")

# Step 2: eportal -> 302 sso authorize
s, h, b = fetch(eportal)
loc = h.get('Location') or h.get('location')
print(f"[2] eportal -> {loc[:80]}...")

# Step 3: sso authorize -> 302 cas/login
s, h, b = fetch(loc)
loc2 = h.get('Location') or h.get('location')
print(f"[3] sso authorize -> {loc2[:80]}...")

# Step 4: cas/login，解析 sig_request
s, h, b = fetch(loc2)
print(f"[4] cas/login status={s} len={len(b)}")
msig = re.search(r'sig_request\s*[:=]\s*["\']([^"\']+)["\']', b)
sig = msig.group(1)
# 用 : 拆 cims_sig / app_sig
cims_sig, app_sig = sig.split(':', 1)
print(f"    cims_sig: {cims_sig}")
print(f"    app_sig: {app_sig}")

# 提取 service（appUrl）：cas/login URL 的 service 参数
from urllib.parse import urlparse, parse_qs
q = parse_qs(urlparse(loc2).query)
service = q['service'][0]
print(f"    service(appUrl): {service[:80]}...")

# Step 5: 构造 iframe URL，访问拿 iframe session
parent_url = loc2  # cas/login URL
iframe_url = "https://sso.hunau.edu.cn/authn/login.html?view=frame&type=0&sign=" + urllib.parse.quote(cims_sig, safe='') + "&parent=" + urllib.parse.quote(parent_url, safe='') + "&version=23423"
print(f"\n[5] iframe url: {iframe_url[:100]}...")
s, h, b = fetch(iframe_url)
print(f"    iframe status={s} len={len(b)}")
# 看 iframe 是否正常加载（应含 loginways8 等）
if 'loginways' in b or 'CimsEncryptor' in b or 'cims' in b.lower():
    print("    iframe OK (含登录表单)")
else:
    print("    iframe head:", b[:200])

# Step 6: POST api/verify/othertype
print(f"\n[6] POST othertype")
api_url = "https://sso.hunau.edu.cn/authn/api/verify/othertype"
form = urllib.parse.urlencode({
    'sign': cims_sig,
    'username': USERNAME,
    'type': '8',
    'appUrl': service,
}).encode()
s, h, b = fetch(api_url, method='POST', data=form,
                headers={'Content-Type':'application/x-www-form-urlencoded',
                         'Referer': iframe_url,
                         'Origin': 'https://sso.hunau.edu.cn'})
print(f"    status={s}")
print(f"    body: {b[:600]}")
try:
    j = json.loads(b)
    print(f"\n=== parsed ===")
    print(f"status: {j.get('status')}")
    rb = j.get('response_body', {})
    print(f"publicKey: {rb.get('publicKey','')[:80]}")
    print(f"encryptor: {rb.get('encryptor')}")
    print(f"token: {rb.get('token','')[:40]}")
except Exception as e:
    print(f"json parse err: {e}")

print("\n=== cookies ===")
for c in cj:
    print(f"{c.name}={c.value[:40]} domain={c.domain}")
