#!/usr/bin/env python3
# 完整认证链：eportal -> sso -> cas/login -> CIMS othertype -> RSA加密 -> checkAuthcode -> cas POST -> code -> eportal callback
import urllib.request, urllib.parse, http.cookiejar, ssl, re, json, subprocess, base64, tempfile, os, sys

USERNAME = "你的学号"
PASSWORD = "你的密码"

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
    if method == 'POST':
        print(f"    [REQ] POST {url[:100]}")
        print(f"    [REQ] headers: {dict(req.headers)}")
        print(f"    [REQ] body: {data[:400] if data else None}")
    try:
        resp = opener.open(req, timeout=20)
        return resp.status, dict(resp.headers), resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode('utf-8', 'replace')

def rsa_encrypt(publicKey_b64, plaintext):
    # publicKey 是 X.509 SubjectPublicKeyInfo PEM body
    pub = publicKey_b64.replace(' ', '').replace('\r','').replace('\n','')
    # 重新格式化成 64 字符行
    lines = [pub[i:i+64] for i in range(0, len(pub), 64)]
    pem = "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n"
    fd, keyfile = tempfile.mkstemp(suffix='.pem')
    os.write(fd, pem.encode()); os.close(fd)
    try:
        r = subprocess.run(['openssl','pkeyutl','-encrypt','-pubin','-inkey',keyfile,
                            '-pkeyopt','rsa_padding_mode:pkcs1'],
                           input=plaintext.encode(), capture_output=True)
        if r.returncode != 0:
            raise RuntimeError("openssl: " + r.stderr.decode(errors='replace'))
        return base64.b64encode(r.stdout).decode()
    finally:
        os.unlink(keyfile)

# === Step 1: eportal redirect ===
s, h, b = fetch("http://1.1.1.1/")
m = re.search(r"http://10\.100\.0\.12/eportal/[a-z]+\.jsp\?[^']+", b)
eportal = m.group(0)
print(f"[1] eportal redirect OK")

# === Step 2: eportal -> sso authorize ===
s, h, b = fetch(eportal)
loc = h.get('Location') or h.get('location')
print(f"[2] eportal -> sso authorize")

# === Step 3: sso authorize -> cas/login ===
s, h, b = fetch(loc)
loc2 = h.get('Location') or h.get('location')
print(f"[3] sso authorize -> cas/login")

# === Step 4: 解析 cas/login ===
s, h, cas_body = fetch(loc2)
print(f"[4] cas/login status={s} len={len(cas_body)}")
msig = re.search(r'sig_request\s*[:=]\s*["\']([^"\']+)["\']', cas_body)
sig = msig.group(1)
cims_sig, app_sig = sig.split(':', 1)
print(f"    cims_sig: {cims_sig[:40]}...")
print(f"    app_sig: {app_sig[:40]}...")

# postaction（form action）
mpa = re.search(r'postaction\s*[:=]\s*["\']([^"\']+)["\']', cas_body)
postaction = mpa.group(1).replace('\\/', '/').replace('\\u0026','&') if mpa else loc2
print(f"    postaction: {postaction}")

# postArgument（form POST 时 sig_response 用的字段名，CIMS.js go 函数 line 52）
# 默认 'sig_response'，但本系统 CIMS.init 显式设为 'signedCimsResponse'
mpa_arg = re.search(r'postArgument\s*:\s*[\'"]([^\'"]+)[\'"]', cas_body)
post_argument = mpa_arg.group(1) if mpa_arg else 'sig_response'
print(f"    postArgument: {post_argument}")

# form hidden fields: 抓所有 input 的 name/value
hidden = {}
for mm in re.finditer(r'<input[^>]*>', cas_body):
    tag = mm.group(0)
    nm = re.search(r'name="([^"]+)"', tag)
    vm = re.search(r'value="([^"]*)"', tag)
    if nm and vm:
        hidden[nm.group(1)] = vm.group(1)
print(f"    hidden keys: {list(hidden.keys())}")

# [DIAG] 打印所有 input 标签完整 HTML
print(f"    [DIAG] all input tags:")
for mm in re.finditer(r'<input[^>]*/?>', cas_body):
    print(f"      {mm.group(0)}")
# [DIAG] 打印 form 标签
form_match = re.search(r'<form[^>]*>', cas_body)
if form_match:
    print(f"    [DIAG] form open tag: {form_match.group(0)}")
# [DIAG] 打印 CIMS.init 完整配置
minit = re.search(r'CIMS\.init\s*\(\s*\{([^}]+)\}', cas_body, re.DOTALL)
if minit:
    print(f"    [DIAG] CIMS.init config:\n      {minit.group(1)[:800]}")
# [DIAG] 打印 hidden 字段值
for k,v in hidden.items():
    print(f"    [DIAG] hidden[{k}] = {v[:80]}")

# service (appUrl)
q = urllib.parse.parse_qs(urllib.parse.urlparse(loc2).query)
service = q['service'][0]

# === Step 5: iframe 拿 session ===
parent_url = loc2
iframe_url = "https://sso.hunau.edu.cn/authn/login.html?view=frame&type=0&sign=" + urllib.parse.quote(cims_sig, safe='') + "&parent=" + urllib.parse.quote(parent_url, safe='') + "&version=23423"
s, h, b = fetch(iframe_url)
print(f"[5] iframe status={s} len={len(b)} Set-Cookie={h.get('Set-Cookie') or h.get('set-cookie')}")

# === Step 6: othertype ===
api = "https://sso.hunau.edu.cn/authn/api/verify/othertype"
form = urllib.parse.urlencode({'sign':cims_sig,'username':USERNAME,'type':'8','appUrl':service}).encode()
s, h, b = fetch(api, method='POST', data=form,
                headers={'Content-Type':'application/x-www-form-urlencoded','Referer':iframe_url,'Origin':'https://sso.hunau.edu.cn'})
j = json.loads(b)
print(f"[6] othertype status={j.get('status')}")
rb = j['response_body']
enc = rb['encryptor']; pubkey = rb['publicKey']; token = rb['token']
print(f"    encryptor={enc} token={token}")

# === Step 7: RSA 加密 password|token ===
if enc != 'INTERNATIONAL':
    print(f"!! encryptor={enc}, 暂只支持 INTERNATIONAL"); sys.exit(1)
authcode = rsa_encrypt(pubkey, PASSWORD + "|" + token)
print(f"[7] RSA encrypted authcode: {authcode[:40]}...")

# === Step 8: checkAuthcode ===
import uuid
uid = ''.join(hex(ord(c)//16)[2:]+hex(ord(c)%16)[2:] for c in str(uuid.uuid4()))[:32]
form2 = urllib.parse.urlencode({
    'sign':cims_sig,'token':token,'authcode':authcode,'type':'8',
    'username':USERNAME,'vericode':'','verificationcode':'','uuid':uid,'appUrl':service,
}).encode()
s, h, b = fetch("https://sso.hunau.edu.cn/authn/api/verify/checkAuthcode", method='POST', data=form2,
                headers={'Content-Type':'application/x-www-form-urlencoded','Referer':iframe_url,'Origin':'https://sso.hunau.edu.cn'})
j2 = json.loads(b)
print(f"[8] checkAuthcode status={j2.get('status')}")
usersign = j2.get('response_body',{}).get('usersign')
print(f"    usersign: {usersign}")
if not usersign:
    print(f"    full: {b[:500]}")
    sys.exit(1)

# === Step 9: POST cas/login form ===
sig_response = usersign + ":" + app_sig
postdata = {post_argument: sig_response}
postdata.update(hidden)
# 确保 username 字段
if 'username' not in postdata: postdata['username'] = ''
postdata['geolocation'] = ''  # form 有 geolocation 隐藏字段（无 value），补上
print(f"[9] POST cas/login form (sig_response={sig_response[:40]}...)")
print(f"    [DIAG] post URL={loc2}")
print(f"    [DIAG] postdata keys={list(postdata.keys())}")
print(f"    [DIAG] postdata sig_response full={sig_response}")
s, h, b = fetch(loc2, method='POST',
    data=urllib.parse.urlencode(postdata).encode(),
    headers={'Content-Type':'application/x-www-form-urlencoded','Referer':loc2,'Origin':'https://sso.hunau.edu.cn'})
loc3 = h.get('Location') or h.get('location')
print(f"    status={s}")
print(f"    [DIAG] response headers:")
for k,v in h.items():
    print(f"      {k}: {v[:200]}")
print(f"    Location={loc3[:200] if loc3 else None}")
print(f"    [DIAG] body[:1500]: {b[:1500]}")

# === Step 10: 跟 redirect 拿 code ===
cur = loc3
eportal_callback = None
for i in range(6):
    if not cur: break
    s, h, b = fetch(cur)
    print(f"[10.{i}] {cur[:80]}... -> status={s}")
    nl = h.get('Location') or h.get('location')
    if 'code=' in cur or 'code=' in (nl or ''):
        mm = re.search(r'[?&]code=(OC-[\w\-]+)', cur) or re.search(r'[?&]code=(OC-[\w\-]+)', nl or '')
        if mm:
            print(f"    *** GOT CODE: {mm.group(1)} ***")
            eportal_callback = nl  # CAS 把 code 加到 redirect_uri（即 eportal URL），走 eth0 访问完成认证
            break
    if not nl: break
    cur = nl

# === Step 11: 访问 eportal callback URL（走 eth0）完成认证 ===
if eportal_callback:
    print(f"[11] eportal callback URL: {eportal_callback[:120]}...")
    s, h, b = fetch(eportal_callback)
    print(f"    status={s}")
    print(f"    body[:1500]: {b[:1500]}")
else:
    print(f"[11] !! 未拿到 eportal callback URL，认证未完成")

print("\n=== cookies ===")
for c in cj:
    print(f"{c.name}={c.value[:40]} domain={c.domain} path={c.path}")
