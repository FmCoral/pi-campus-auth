#!/usr/bin/env python3
# 校园网自动认证 - 生产版（纯 Python，零人工干预）
#
# 部署位置：/root/auto_auth.py
# 账号配置：/root/auto_auth.conf（从 auto_auth.conf.example 复制；不入代码库）
#           也支持环境变量 CAMPUS_USERNAME / CAMPUS_PASSWORD（优先级更高）
# cron 配置：* * * * * /root/auto_auth.py >> /root/auto_auth.log 2>&1（日志持久化在 /root）
# 触发逻辑：每分钟运行一次，先 ping 8.8.8.8 检测是否掉线，掉线则跑完整认证流程
# 认证链：eportal redirect → sso authorize → cas/login → CIMS othertype → RSA加密 → checkAuthcode → cas POST → code → eportal callback

import urllib.request, urllib.parse, http.cookiejar, ssl, re, json
import subprocess, base64, tempfile, os, sys, time, uuid

# 凭据配置文件路径（部署时与脚本同目录 /root）
CONF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'auto_auth.conf')
PROBE_HOST = "8.8.8.8"
PROBE_TIMEOUT = 3  # ping 超时秒数


def load_credentials():
    """读取校园网账号密码：环境变量优先，其次 auto_auth.conf
    返回 (username, password)，缺失返回 (None, None)"""
    conf = {}
    if os.path.exists(CONF_PATH):
        try:
            with open(CONF_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, v = line.split('=', 1)
                        conf[k.strip()] = v.strip()
        except Exception:
            pass
    username = os.environ.get('CAMPUS_USERNAME') or conf.get('USERNAME')
    password = os.environ.get('CAMPUS_PASSWORD') or conf.get('PASSWORD')
    return username, password

# 树莓派 musl libc 不校验 SSL，关掉避免 ECDSA 弱密钥问题
ctx = ssl._create_unverified_context()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用自动跟随 302，让 fetch 能拿到 Location 头"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def make_opener():
    """构造 opener + cookiejar，每次认证用新会话"""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=ctx),
        NoRedirect,
    )
    opener.addheaders = [('User-Agent', 'Mozilla/5.0')]
    return opener, cj


def fetch(opener, url, method='GET', data=None, headers=None, timeout=20):
    h = {'User-Agent': 'Mozilla/5.0'}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, method=method, data=data, headers=h)
    try:
        resp = opener.open(req, timeout=timeout)
        return resp.status, dict(resp.headers), resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        # HTTPError 也要拿到 body 和 headers，CAS 在 302 时会被 NoRedirect 抛 HTTPError
        return e.code, dict(e.headers), e.read().decode('utf-8', 'replace')
    except urllib.error.URLError as e:
        raise RuntimeError(f"URLError {e}")


def rsa_encrypt(public_key_b64, plaintext):
    """RSA PKCS1v1.5 加密：明文 = password + "|" + token"""
    pub = public_key_b64.replace(' ', '').replace('\r', '').replace('\n', '')
    lines = [pub[i:i + 64] for i in range(0, len(pub), 64)]
    pem = "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n"
    fd, keyfile = tempfile.mkstemp(suffix='.pem')
    os.write(fd, pem.encode())
    os.close(fd)
    try:
        r = subprocess.run(
            ['openssl', 'pkeyutl', '-encrypt', '-pubin', '-inkey', keyfile,
             '-pkeyopt', 'rsa_padding_mode:pkcs1'],
            input=plaintext.encode(), capture_output=True
        )
        if r.returncode != 0:
            raise RuntimeError("openssl: " + r.stderr.decode(errors='replace'))
        return base64.b64encode(r.stdout).decode()
    finally:
        os.unlink(keyfile)


def is_online():
    """探测 eth0 是否还能上网：ping 公网 IP 通则在线"""
    try:
        r = subprocess.run(
            ['ping', '-c', '1', '-W', str(PROBE_TIMEOUT), PROBE_HOST],
            capture_output=True, timeout=PROBE_TIMEOUT + 3
        )
        return r.returncode == 0
    except Exception:
        return False


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def authenticate(username, password):
    """完整认证流程，返回 (True, msg) / (False, msg)"""
    opener, cj = make_opener()
    try:
        # === Step 1: eportal redirect（校园网关劫持 HTTP）===
        s, h, b = fetch(opener, "http://1.1.1.1/")
        m = re.search(r"http://10\.100\.0\.12/eportal/[a-z]+\.jsp\?[^']+", b)
        if not m:
            return False, "step1: no eportal redirect (already online?)"
        eportal = m.group(0)

        # === Step 2-3: eportal → sso authorize → cas/login ===
        s, h, b = fetch(opener, eportal)
        loc = h.get('Location') or h.get('location')
        if not loc:
            return False, f"step2: no sso redirect (status={s})"

        s, h, b = fetch(opener, loc)
        loc2 = h.get('Location') or h.get('location')
        if not loc2:
            return False, f"step3: no cas/login redirect (status={s})"

        # === Step 4: 解析 cas/login（提取 sig_request, postArgument, hidden 字段）===
        s, h, cas_body = fetch(opener, loc2)
        msig = re.search(r'sig_request\s*[:=]\s*["\']([^"\']+)["\']', cas_body)
        if not msig:
            return False, "step4: no sig_request in cas/login"
        sig = msig.group(1)
        cims_sig, app_sig = sig.split(':', 1)

        # postArgument 是 POST cas/login 时 sig_response 用的字段名
        # CIMS.js go 函数 line 52: input.name = options.postArgument
        # 默认 'sig_response'，本系统 CIMS.init 显式设为 'signedCimsResponse'
        mpa = re.search(r'postArgument\s*:\s*[\'"]([^\'"]+)[\'"]', cas_body)
        post_argument = mpa.group(1) if mpa else 'sig_response'

        # 提取所有 hidden input（execution, _eventId, username honeypot=aaaaaaa）
        hidden = {}
        for mm in re.finditer(r'<input[^>]*>', cas_body):
            tag = mm.group(0)
            nm = re.search(r'name="([^"]+)"', tag)
            vm = re.search(r'value="([^"]*)"', tag)
            if nm and vm:
                hidden[nm.group(1)] = vm.group(1)

        q = urllib.parse.parse_qs(urllib.parse.urlparse(loc2).query)
        if 'service' not in q:
            return False, "step4: no service in cas URL"
        service = q['service'][0]

        # === Step 5: iframe 拿 session（authn 域，path=/authn/，不发 CAS_SESSION）===
        iframe_url = (
            "https://sso.hunau.edu.cn/authn/login.html?view=frame&type=0&sign="
            + urllib.parse.quote(cims_sig, safe='')
            + "&parent=" + urllib.parse.quote(loc2, safe='')
            + "&version=23423"
        )
        s, h, b = fetch(opener, iframe_url)

        # === Step 6: othertype（拿 encryptor/publicKey/token）===
        form = urllib.parse.urlencode({
            'sign': cims_sig, 'username': username, 'type': '8', 'appUrl': service
        }).encode()
        s, h, b = fetch(opener, "https://sso.hunau.edu.cn/authn/api/verify/othertype",
                        method='POST', data=form,
                        headers={'Content-Type': 'application/x-www-form-urlencoded',
                                 'Referer': iframe_url,
                                 'Origin': 'https://sso.hunau.edu.cn'})
        j = json.loads(b)
        if j.get('status') != 1000:
            return False, f"step6: othertype status={j.get('status')} body={b[:200]}"
        rb = j['response_body']
        enc = rb['encryptor']
        pubkey = rb['publicKey']
        token = rb['token']
        if enc != 'INTERNATIONAL':
            return False, f"step6: unsupported encryptor={enc}"

        # === Step 7: RSA 加密 password|token（不是 password 本身！）===
        authcode = rsa_encrypt(pubkey, password + "|" + token)

        # === Step 8: checkAuthcode → usersign ===
        uid = uuid.uuid4().hex  # 32 位 hex 随机串
        form2 = urllib.parse.urlencode({
            'sign': cims_sig, 'token': token, 'authcode': authcode, 'type': '8',
            'username': username, 'vericode': '', 'verificationcode': '',
            'uuid': uid, 'appUrl': service,
        }).encode()
        s, h, b = fetch(opener, "https://sso.hunau.edu.cn/authn/api/verify/checkAuthcode",
                        method='POST', data=form2,
                        headers={'Content-Type': 'application/x-www-form-urlencoded',
                                 'Referer': iframe_url,
                                 'Origin': 'https://sso.hunau.edu.cn'})
        j2 = json.loads(b)
        if j2.get('status') != 1000:
            return False, f"step8: checkAuthcode status={j2.get('status')} body={b[:200]}"
        usersign = j2.get('response_body', {}).get('usersign')
        if not usersign:
            return False, f"step8: no usersign in response"

        # === Step 9: POST cas/login form（字段名用 postArgument，不是 sig_response！）===
        sig_response = usersign + ":" + app_sig
        postdata = {post_argument: sig_response}
        postdata.update(hidden)
        if 'username' not in postdata:
            postdata['username'] = ''
        postdata['geolocation'] = ''  # form 有 geolocation 隐藏字段（无 value），补上
        s, h, b = fetch(opener, loc2, method='POST',
                       data=urllib.parse.urlencode(postdata).encode(),
                       headers={'Content-Type': 'application/x-www-form-urlencoded',
                                'Referer': loc2,
                                'Origin': 'https://sso.hunau.edu.cn'})
        loc3 = h.get('Location') or h.get('location')
        if not loc3:
            return False, f"step9: no cas redirect (status={s}, probably Invalid credentials)"

        # === Step 10: 跟 redirect 链拿 code（callbackAuthorize → authorize → redirect_uri?code=）===
        cur = loc3
        eportal_callback = None
        for i in range(6):
            if not cur:
                break
            s, h, b = fetch(opener, cur)
            nl = h.get('Location') or h.get('location')
            if 'code=' in cur or 'code=' in (nl or ''):
                mm = re.search(r'[?&]code=(OC-[\w\-]+)', cur) \
                    or re.search(r'[?&]code=(OC-[\w\-]+)', nl or '')
                if mm:
                    eportal_callback = nl
                    break
            if not nl:
                break
            cur = nl

        if not eportal_callback:
            return False, "step10: no code in redirect chain"

        # === Step 11: 访问 eportal callback URL（走 eth0）完成认证 ===
        s, h, b = fetch(opener, eportal_callback)

        # 二次验证：认证后 ping 应该通
        time.sleep(1)
        if is_online():
            return True, "ok"
        return True, "ok (eportal callback done, ping pending)"

    except Exception as e:
        return False, f"exception: {type(e).__name__}: {e}"


def main():
    username, password = load_credentials()
    if not username or not password:
        log(f"credentials missing: fill {CONF_PATH} or set CAMPUS_USERNAME/CAMPUS_PASSWORD")
        sys.exit(2)
    if is_online():
        # 已认证，cron 静默退出
        return
    log("offline detected, starting re-auth...")
    ok, msg = authenticate(username, password)
    if ok:
        log(f"re-auth SUCCESS: {msg}")
    else:
        log(f"re-auth FAIL: {msg}")
        sys.exit(1)


if __name__ == '__main__':
    main()
