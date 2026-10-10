# 树莓派校园网自动认证 + 热点 HWIFI 完全部署手册

> 面向后来者的可复刻文档：从零搭建的完整步骤、逆向细节、全部踩过的坑。
> 环境：湖南农业大学校园网（锐捷 ePortal + CAS + Trusfort CIMS）
> 初版：2026-09-25 ｜ 最近大改：2026-10-10

### 2026-10-10 重要修订（先读）

早期文档里的三条结论已被实测推翻，部署时**不要再照做**：

| 旧结论（已废弃） | 事实（2026-10-10 验证） |
|---|---|
| SSO 固定是 `61.187.55.38`，要写进 /etc/hosts | 那是 SSO 的**旧公网地址**，写死 hosts 反而导致走错门被 MITM；已删除 |
| 校园 DNS `10.10.10.10` 对 SSO 只返回 v6 | 实际返回**校内 v4 `10.8.9.35`**（合法证书、未认证白名单可达） |
| 掉线需要切 coral 热点才能认证 | eth0 直达校内 SSO 即可，**无需热点、第三台设备或任何人工动作** |

---

# 第一部分：项目概述

## 1.1 目标

用一台树莓派实现：

1. **网口接校园网**，开机自动完成网页认证（锐捷 ePortal + SSO CAS + CIMS 二次认证）
2. **板载 WiFi 广播热点 HWIFI**，手机/电脑等终端接入即可上网
3. **掉线自动重认证**，全程零人工干预，不增加额外硬件
4. **规避校园网"防私接/防代理"检测**
5. 提供 **Web 监控面板** 与可选 **OLED 状态屏**，支持定时/立即安全关机

## 1.2 最终网络拓扑

```
[校园网墙插] ──网线── [Pi eth0 / WAN]  DHCP 动态获取（示例 10.101.0.25/20，gw 10.101.0.1）
                       │   默认路由走这里；自动认证
                       │
                       [Pi br-lan]      192.168.1.1/24（DHCP：.100~.249）
                       ▲
                       [Pi phy0-ap0]    5GHz channel 149 VHT80
                       SSID=HWIFI  加密=WPA2-PSK
                       ▲
        ┌──────────────┼──────────────┐
   [手机]          [电脑]        [其他终端]        ← 终端经 NAT 上网
```

**关键设计**：终端流量经 NAT（masquerade）后，校园网只看到树莓派一个 IP + 一个 MAC + 一个账号，等同于一台普通设备。

## 1.3 硬件 / 软件环境

| 项 | 实际版本/参数 |
|----|----|
| 板子 | 树莓派 4B（bcm2711，4 核 Cortex-A72，aarch64） |
| 系统 | **ImmortalWrt 25.12.2** r38135（target bcm27xx/bcm2711） |
| libc | musl（注意：不是 glibc，很多 x86 软件直接跑不了） |
| Python | python3 3.13.9 |
| openssl | openssl-util 3.5.7（RSA 加密用） |
| TLS 库 | libustream-openssl + ca-bundle |
| WiFi 芯片 | BCM43455（板载 SDIO），固件 7.45.250（2022-07-29） |
| 控制电脑 | Windows 10/11 + PowerShell（另备 WSL2 Ubuntu-22.04 救砖用） |
| OLED（可选） | 0.96 寸 SSD1306 128×64 I2C，地址 0x3C |

## 1.4 校园网关键地址（2026-10-10 实测）

| 名称 | 地址 | 说明 |
|------|------|------|
| ePortal 服务器 | `http://10.100.0.12` | 锐捷认证网关（内网 HTTP） |
| SSO 校内地址 | `10.8.9.35` | **校园 DNS 返回，未认证白名单可达，正确入口** |
| SSO 当前公网地址 | `218.104.146.107` | 公共 DNS 返回（合法 TrustAsia 证书） |
| SSO 旧公网地址 | `61.187.55.38` | 未认证时走它会被网关 MITM（锐捷伪证书）——**避开** |
| SSO v6 | `2408:8653:21::a001:7` | 校园网不给 v6 前缀，用不上 |
| 校园 DNS | `10.10.10.10` | SSO → 10.8.9.35 |
| 备用 DNS | `114.114.114.114` | WAN DHCP 下发的第二上游 |
| 探测目标 | `223.5.5.5` / `180.76.76.76` / `8.8.8.8` | 任一通则网络层可达 |

> SSO 三个 v4 地址的证书核对：10.8.9.35 与 218.104.146.107 均为 TrustAsia OV 签发的 `*.hunau.edu.cn` 合法证书；61.187.55.38 在未认证时返回锐捷（ruijie.com.cn）伪证书。

---

# 第二部分：从零复刻

## 2.1 烧录系统

1. 到 ImmortalWrt 官网下载 **bcm27xx/bcm2711** 的 factory 镜像（`.img.gz`），解压出 `.img`
2. 用 Rufus（选 DD 模式）或 balenaEtcher 烧入 SD 卡
3. 插卡上电，首次启动后默认 LAN 地址 `192.168.1.1`（root 默认空密码）
   - 调试期可用网线直连电脑，或接显示器 + 键盘

> ⚠️ 树莓派 4B 默认 eth0 挂在 LAN 桥上。我们要把 eth0 改成 WAN，见 2.2。

## 2.2 网络配置 /etc/config/network

最终内容（已实测）：

```sh
config interface 'loopback'
        option device 'lo'
        option proto 'static'
        list ipaddr '127.0.0.1/8'

config device
        option name 'br-lan'
        option type 'bridge'          # 空桥：AP 启用后自动成为成员

config interface 'lan'
        option device 'br-lan'
        option proto 'static'
        list ipaddr '192.168.1.1/24'
        option ip6assign '60'

config interface 'wan'
        option proto 'dhcp'
        option device 'eth0'         # eth0 做 WAN，DHCP 拿校园网 IP

config interface 'wan6'
        option proto 'dhcpv6'
        option device 'eth0'
        option reqaddress 'try'
        option reqprefix 'auto'      # 校园网不给 v6，一直 pending（无害）

# wwan（连 coral 热点）当前架构不再使用；保留接口但 disabled
config interface 'wwan'
        option proto 'dhcp'
```

uci 方式：

```sh
uci set network.wan=interface
uci set network.wan.proto='dhcp'
uci set network.wan.device='eth0'
uci commit network
```

## 2.3 防火墙 /etc/config/firewall

关键部分（其余默认规则保留）：

```sh
config defaults
        option input 'REJECT'
        option forward 'REJECT'
        option fullcone '1'              # 全锥 NAT（NAT 类型友好）

config zone
        option name 'lan'
        list network 'lan'
        list network 'wwan'
        option input 'ACCEPT'
        option output 'ACCEPT'
        option forward 'ACCEPT'

config zone
        option name 'wan'
        list network 'wan'
        list network 'wan6'
        option input 'REJECT'            # eth0 上 SSH / 8080 都被拦
        option forward 'DROP'
        option masq '1'                  # ★ NAT：终端用树莓派 IP 出去
        option mtu_fix '1'

config forwarding
        option src 'lan'
        option dest 'wan'                # ★ 允许终端转发到校园网
```

> SSH 和监控面板因此**只能从 HWIFI（LAN）访问**，这是有意的安全边界。

## 2.4 DHCP 服务（给热点终端分配 IP）

`/etc/config/dhcp` 的 lan 段确认如下：

```sh
config dhcp 'lan'
        option interface 'lan'
        option start '100'               # 192.168.1.100 起
        option limit '150'               # 到 .249
        option leasetime '12h'
        option dhcpv4 'server'
```

dnsmasq 默认随系统启动，无需额外操作。

---

## 2.5 认证系统分析（逆向前必读）

该校认证是三段式：

```
锐捷 ePortal（网关）  →  CAS OAuth2.0（发授权码）  →  Trusfort CIMS（二次身份认证）
```

**关键认知**：普通"账号密码 POST"拿不到登录态——CAS 登录页嵌了一个 CIMS iframe，密码要在 iframe 里经 RSA 加密、走 CIMS 独立 API 换成 `usersign`，再由父页面回填表单 POST 给 CAS。这就是简单脚本登录失败的原因。

### 逆向方法论

1. 电脑接一个能正常上网的网络，浏览器 F12 → Network，勾选 Preserve log
2. 在未认证网口访问任意 `http://` 站点，观察完整跳转链
3. 右键保存登录页 HTML（本项目存在 reference/post.html）
4. 从页面下载关键 JS：`CIMS.js`（父窗口逻辑）、`cims_web.js`（iframe API）、`cims_encryptor.js`（加密选择）
5. 在 JS 里搜字段名、API 路径、encrypt 调用，逐步还原

---

## 2.6 认证链路完整逆向（11 步）

```
校园网关劫持 HTTP → eportal redirect
        │
 Step 1  GET http://1.1.1.1/
         网关返回 200 + 一段 JS（不是 302！），正则提取：
         http://10.100.0.12/eportal/index.jsp?wlanuserip=<加密串>&...
         wlanuserip 等是本机 IP/MAC 的加密参数，每次掉线必须重新拿
        │ 走 eth0
 Step 2  GET eportal index.jsp
         → 302 到 sso /oauth2.0/authorize（redirect_uri 含 wlanuserip）
        │
 Step 3  GET sso authorize
         → 302 到 /cas/login?service=https://sso.../callbackAuthorize?...
        │
 Step 4  GET cas/login → 200 登录页（可能是 gzip 压缩，见 2.12 与 3.3），提取：
         • sig_request  格式  XD|<b64>|<hex>:APP|<b64>|<hex>
                        以 ':' 分两半：cims_sig（左，XD| 前缀）/ app_sig（右，APP|）
         • postArgument CIMS.init 配置的 POST 字段名（★见 2.7）
         • hidden 表单  execution, _eventId, username(蜜罐=aaaaaaa), geolocation
         • service      从 URL query 取（= callbackAuthorize URL）
        │
 Step 5  GET iframe（建立 authn 会话）
         https://sso/authn/login.html?view=frame&type=0
               &sign=<cims_sig URL编码>&parent=<cas/login URL编码>&version=23423
        │
 Step 6  POST /authn/api/verify/othertype
         body: sign, username=学号, type=8, appUrl=service
         → {"status":1000,"response_body":
              {"encryptor":"INTERNATIONAL","publicKey":"MIGfMA0...","token":"<hex32>"}}
         encryptor=INTERNATIONAL 表示用 RSA（非国密 SM2）
        │ 本地
 Step 7  RSA 加密
         publicKey 是 X.509 SubjectPublicKeyInfo 的 base64 body，拼 PEM（每行 64 字符）
         ★ 明文 = password + "|" + token（不是密码本身！）
         padding = PKCS1v1.5
         openssl pkeyutl -encrypt -pubin -inkey key.pem \
                 -pkeyopt rsa_padding_mode:pkcs1  → base64 = authcode
        │
 Step 8  POST /authn/api/verify/checkAuthcode
         body: sign, token, authcode, type=8, username=学号,
               vericode='', verificationcode='', uuid=<32位hex随机>, appUrl=service
         → {"status":1000,"response_body":{"usersign":"AUTH|<b64>|<hex40>"}}
        │
 Step 9  POST cas/login 表单
         sig_response = usersign + ":" + app_sig
         字段名必须用 postArgument（本系统 = signedCimsResponse）
         → 302 + Set-Cookie TGC + Location=callbackAuthorize?ticket=ST-xxx
        │
 Step 10 跟随跳转链 callbackAuthorize → authorize
         → 302 到 redirect_uri?code=OC-xxxxx，记下该 URL
        │ 走 eth0
 Step 11 GET eportal callback（带 code 的 URL）
         eportal 标记该 IP 已认证 → eth0 可上网
```

## 2.7 最致命的坑之一：POST 字段名（Step 9 的 401）

CIMS.js 的 `go()` 里回填字段名**不是写死的**：

```js
input.name = options.postArgument;     // 取自 CIMS.init 配置
input.value = response + ':' + appSig;
```

默认值是 `sig_response`，**但本系统登录页显式覆盖**为 `signedCimsResponse`。用默认名提交 → 字段缺失 → **401 Invalid credentials**。

正确做法是动态解析、不要硬编码：

```python
mpa = re.search(r'postArgument\s*:\s*[\'"]([^\'"]+)[\'"]', cas_body)
post_argument = mpa.group(1) if mpa else 'sig_response'
postdata = {post_argument: sig_response}
```

> 排查时曾怀疑 cookie / Referer / IP / URL 编码，全部证伪。
> 教训：**先核对前端 JS 的字段名，再怀疑网络层。**

## 2.8 前端 JS 逆向要点

| 文件 | 关键内容 |
|------|---------|
| CIMS.js | `parseSigRequest` 拆 sig；`onReceivedMessage` 收 iframe 消息；`go()` 回填 submit；postArgument 默认值 |
| cims_web.js | othertype 请求；checkAuthcode，内部 `encrypt(publicKey, password+"|"+token)`；成功后 postMessage 发 `GO|usersign` |
| cims_encryptor.js | encryptor=INTERNATIONAL → JSEncrypt（RSA PKCS1v1.5） |

postMessage 格式：`GO|AUTH|<base64>|<hex>`，父窗口去掉 `GO|` 得到 usersign。

## 2.9 RSA 加密实现

树莓派没有 Python 加密库，直接调系统 openssl：

```python
def rsa_encrypt(public_key_b64, plaintext):
    pub = public_key_b64.replace('\n','').replace('\r','').replace(' ','')
    lines = [pub[i:i+64] for i in range(0, len(pub), 64)]
    pem = "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n"
    # 写临时文件 → openssl pkeyutl -encrypt -pkcs1 → base64
```

完整实现见 [auto_auth.py](../src/auto_auth.py) 的 `rsa_encrypt()`。

## 2.10 关于 TGC 长效 Cookie（故意不复用）

Step 9 成功后 CAS 会发 `TGC=TGT-xxxx`，浏览器靠它跳过 CIMS。脚本**故意每次新建 CookieJar**：TGC 有服务端时效，依赖它会引入"何时需要重新登录"的不确定性；每次走完整链路最可预测。

---

## 2.11 部署生产脚本 auto_auth.py

上传 [auto_auth.py](../src/auto_auth.py) 到 `/root/auto_auth.py`：

| 函数 | 职责 |
|------|------|
| `load_credentials()` | 从 auto_auth.conf / 环境变量读取账号 |
| `is_online()` | ping 多目标 + wget 百度检测 HTTP 劫持 |
| `_decode_body()` | 按 Content-Encoding 解压 gzip/deflate |
| `rsa_encrypt()` | 调 openssl 做 RSA |
| `authenticate()` | 完整 11 步，返回 (bool, msg) |
| `main()` | 在线静默退出；掉线则 authenticate，失败 exit(1) |

部署三件事（行尾/权限坑见第三部分）：

```sh
tr -d '\r' < /root/auto_auth.py > /tmp/c.py && mv /tmp/c.py /root/auto_auth.py  # 去 CRLF
chmod +x /root/auto_auth.py
cp /root/auto_auth.conf.example /root/auto_auth.conf
vi /root/auto_auth.conf        # 真实学号密码；该文件不入库
```

也支持环境变量 `CAMPUS_USERNAME` / `CAMPUS_PASSWORD`（优先级高于 conf）。

## 2.12 ★ DNS 正确配置（2026-10-10 后的正确做法）

目标：让 SSO 域名解析到**校内 10.8.9.35**，而不是任何公网地址。需要三件事：

**① 删除 /etc/hosts 里的历史死绑定（如果存在）：**

```sh
cp /etc/hosts /etc/hosts.bak-mix
sed -i '/sso.hunau.edu.cn/d' /etc/hosts
```

**② 放行 rebind 保护：** dnsmasq 默认 `rebind_protection=1`，会把"域名解析出 RFC1918 内网地址"当成 DNS 投毒丢弃。10.8.9.35 虽是公网段地址不触发它，但保持白名单更稳：

```sh
uci add_list dhcp.@dnsmasq[0].rebind_domain='hunau.edu.cn'
uci commit dhcp
```

**③ 在 /etc/dnsmasq.conf 强制该域名走校园 DNS（可靠落点）：**

```sh
echo "server=/hunau.edu.cn/10.10.10.10" >> /etc/dnsmasq.conf
/etc/init.d/dnsmasq restart
```

验证：

```sh
nslookup sso.hunau.edu.cn 127.0.0.1   # 必须看到 Address: 10.8.9.35
```

> 为什么用 /etc/dnsmasq.conf 而不是 uci？当时实测 `uci add_list ... server='...'` / `address='...'` 的条目**没有落入**生成的 `/var/etc/dnsmasq.conf.cfg01411c`（该版本 dnsmasq 经 ujail 启动，配置生成行为特殊）。/etc/dnsmasq.conf 是主配置里 `conf-file=` 直接引用的，必定生效。排查这类问题的方法：直接看生成文件内容，别只信 uci。

## 2.13 cron 部署（每分钟检测）

`/etc/crontabs/root`（即仓库 [crontab-root](../src/crontab-root)）：

```cron
* * * * * /root/auto_auth.py >> /root/auto_auth.log 2>&1
30 4 * * * [ $(stat -c%s /root/auto_auth.log 2>/dev/null || echo 0) -gt 200000 ] && echo > /root/auto_auth.log
```

第二条：日志超 200KB 自动清空。日志放 `/root`（持久层），不能放 tmpfs。

```sh
/etc/init.d/cron enable
/etc/init.d/cron start      # 前提：crontabs 目录非空（见 3.5）
```

> crond 还有「错过补跑」机制，对关机类任务是陷阱——见 2.19 与 3.9。

## 2.14 部署热点 HWIFI

`/etc/config/wireless` 最终内容：

```sh
config wifi-device 'radio0'
        option type 'mac80211'
        option path 'platform/soc/fe300000.mmcnr/mmc_host/mmc1/mmc1:0001/mmc1:0001:1'
        option band '5g'
        option channel '149'
        option htmode 'VHT80'
        option country 'CN'

config wifi-iface 'default_radio0'
        option device 'radio0'
        option network 'lan'
        option mode 'ap'
        option ssid 'HWIFI'
        option encryption 'psk2'
        option key '热点密码'
        option disabled '0'           # 启用 AP

config wifi-iface 'wwan'
        option device 'radio0'
        option network 'wwan'
        option mode 'sta'
        option ssid 'coral'
        option encryption 'psk2'
        option key '22222222'
        option disabled '1'           # ★ sta 永久关闭（见 2.15）
```

AP 启用后自动加入 br-lan，终端可连接。

## 2.15 ★ sta + AP 并发必须放弃

BCM43455 单 radio 的 sta 和 AP **单独用都稳定，共存必崩**。尽管 `iw phy` 声称支持 `managed<=1, AP<=1`，固件实际有缺陷：

| 尝试 | 结果 |
|---------|------|
| `wifi reload`（sta+AP 同起） | radio 崩、sta 不重连、SSH 断 |
| `wifi reconf`（sta 在线热加 AP） | 整个 radio 挂 |
| `wifi reconf`（AP 在线热加 sta） | AP+sta 全挂 |

dmesg 无 `firmware crashed`（接口能创建），但共存时 sta 连上即被踢、hostapd 反复重启。**结论：AP-only；确需 sta 就加 USB WiFi 棒（独立 phy）。**

## 2.16 TTL 伪装防检测

**原理**：终端包 TTL=64，经 FORWARD 变 63 再 SNAT 出去——网关据此识别"经过路由"。措施：POSTROUTING 钩子把出站 TTL 强制 64。

```sh
# 内核支持性测试
nft add table ip ttlprobe
nft add chain ip ttlprobe c
nft add rule ip ttlprobe c ip ttl set 64
nft delete table ip ttlprobe
```

`/etc/firewall.ttl.sh`（仓库 [firewall.ttl.sh](../src/firewall.ttl.sh)，幂等）：

```sh
#!/bin/sh
nft list table ip ttlfix >/dev/null 2>&1 || nft add table ip ttlfix
nft list chain ip ttlfix postrouting >/dev/null 2>&1 || \
  nft 'add chain ip ttlfix postrouting { type filter hook postrouting priority mangle; policy accept; }'
nft flush chain ip ttlfix postrouting
nft add rule ip ttlfix postrouting oifname eth0 ip ttl set 64
```

注册为 firewall include 实现持久化：

```sh
uci add firewall include
uci set firewall.@include[-1].type='script'
uci set firewall.@include[-1].path='/etc/firewall.ttl.sh'
uci set firewall.@include[-1].reload='1'
uci commit firewall
sh /etc/firewall.ttl.sh
```

> 本内核不支持 `route` 类型链，必须用 `filter` 挂 postrouting（见 3.6）。

**验证**：用原始 socket 发 TTL=1 的 ICMP——无规则时包死在第一跳；有规则时出口改 64、收到 echo reply。脚本见 [verify_ttl.py](../src/verify_ttl.py)。

## 2.17 OLED 状态屏 + 按设备流量统计

### 接线（务必照此，看错排会拉死总线）

| OLED | 物理引脚 | 说明 |
|------|---------|------|
| VCC | 引脚 1 | 3.3V |
| GND | 引脚 9 | GND |
| SDA | 引脚 3 | GPIO2（硬件 I2C） |
| SCL | 引脚 5 | GPIO3 |

启用硬件 I2C，驱动纯标准库（fcntl + os.write /dev/i2c-1），无需 smbus。总线卡死时给 9 个 SCL 脉冲解锁。

### 屏幕页面（约 0.5s 刷新）

- 页1 网络：连接数、实时上/下行速率、整机累计流量
- 页2 系统：校园网连通、CPU 温度、内存、CPU 占用
- 页3 设备：每台设备名 + 累计流量

中文渲染靠内置 12×12 CJK 字模（仅覆盖用到的字），补字用 [gen_cjk_font.py](../tools/gen_cjk_font.py)。

### 按设备记账（nftables 命名计数器；nlbwmon 方案已否决）

nlbwmon 与 flow offloading 不兼容：开分载后 conntrack 字节恒 0、永远空表；关分载后其 netlink 通道在 6.12 内核仍不产数据。最终方案：

1. 守护进程每 5s 读 `/tmp/dhcp.leases`，为每个租约 IP 在表 `inet hwacct` 建 `up_<IP十六进制>` / `down_<IP十六进制>` 计数器 + forward 规则；租约消失即删
2. 读计数器差值，按 **MAC** 累计（换 IP 不丢账），写 `/root/traffic_state.json`（内含计数器基准，重启不重复计）
3. 每 10 分钟追加快照到 `/root/traffic_log/YYYY-MM-DD.txt`

```sh
# procd 服务 /etc/init.d/oled：
#   procd_set_param command /usr/bin/python3 /root/oled_status.py
#   procd_set_param respawn 3600 5 5
/etc/init.d/oled enable && /etc/init.d/oled start
```

**代价**：必须保持 `flow_offloading=0`（流量走软件转发）。3~5 终端日常流量对 4B 无压力；要千兆线速可删对账逻辑后重开 offload。nft 计数器断电清零，重启后代码检测到负差会重置基线并跳过该轮，确保累计不丢。

## 2.18 Web 监控面板部署

文件：[monitor_web.py](../src/monitor_web.py) + [monitor.html](../src/monitor.html)，部署到 `/root/`。单文件 HTTP 服务，端口 **8080**，仅 LAN。

```sh
# procd 服务 /etc/init.d/monitorweb：
#   procd_set_param command /usr/bin/python3 /root/monitor_web.py
#   procd_set_param respawn 3600 5 5
/etc/init.d/monitorweb enable && /etc/init.d/monitorweb start
```

接口一览：

| 路径 | 内容 |
|------|------|
| `/api/status` | 总览 / 设备 / 系统实时数据 |
| `/api/system` | 分核 CPU、内存明细、存储、接口、进程、温度曲线 |
| `/api/hist?h=24\|72` | 分钟级流量历史（3 天环形） |
| `/api/history?days=30` | 按天流量（traffic_log 差值） |
| `/api/log?lines=200` | 认证日志尾部 |
| `/api/device_dest` | 设备流量去向 |
| `/api/auth_run` | 立即触发一次认证 |
| `/api/shutdown` | 定时/立即关机（立即需密钥 623） |

前端为单文件 HTML，后端每次请求现读，改完浏览器即生效。面板全部速率数字 1 秒采样 + 5 秒滑动窗口平均；速率类图表对数坐标。

### 终端设备页要点

- KPI 四卡：设备总数（在线/离线）、当前在线（在线率）、24h 活跃、最久在线
- 状态副行：在线「已连接 X小时X分」；离线「最后在线 X小时前 · 今天HH:MM」（跨天显日期）
- 累计在线时长列（替换协商速率列），所有列支持表头排序
- 设备行点击展开**流量去向**：环形图（中心总流量）+ 服务列表（最多 7 个，其余并入其他），今日 / 近 7 天切换，展开状态在每秒刷新下保持
- 数据：`/root/device_seen.json`（`{mac:{s:最后在线,t:累计秒}}`，在线每 5s 累加、60s 落盘，离线立即固化）；`/root/traffic_dest.json`（30 天）
- 流量去向通过原始套接字 + BPF 过滤器嗅探 DNS（仅 53 端口，不改 dnsmasq），conntrack 每 10s 做连接字节增量，按「IP→域名→服务商」归属；已排除路由器自身 cloudflared 流量
- 两类数据均从功能上线（2026-10 上旬）起算，不可追溯历史

### 温度趋势

- 标题栏「1 小时 / 24 小时 / 3 天」分段：24h 跨天日期 + 每 4 小时整点；3d 每日日期 + 每 6 小时整点
- Y 轴按数据自适应（不固定上限）；65℃ 告警虚线；档位在刷新/切页后保持
- 1h 点为内存环形（每秒 3600 点，重启清零）；半小时平均点存 `/root/temp30_hist.json`（3 天 144 点，桶闭合即落盘，另有 120s 兜底与关机保存）

## 2.19 电源管理部署与锁文件（重要）

### 功能

- **定时关机**：面板系统页电源卡片控制，默认每日 **23:20**，可开关。实现方式是向 crontab 增删带标记 `hwipi-sched-shutdown` 的行：
  ```cron
  20 23 * * * python3 /root/do_shutdown.py # hwipi-sched-shutdown
  ```
- **立即关机**：面板输入密钥 **623** + 二次确认；空输入/错密钥拒绝
- **关机善后**（[do_shutdown.py](../src/do_shutdown.py)）：
  1. 停 OLED 服务（SIGTERM 触发整机/设备/温度等末次落盘）
  2. 接管屏幕显示「已关机」（看到即可安全拔电源）
  3. 两次 `sync` 后 `poweroff`
- 关机过程记录在 `/root/shutdown.log`

### ★ 必须知道的坑：cron「错过补跑」连环关机

2026-10-08 实测：23:20（当时设定为 13:40）关机成功后，重新上电会被**再次关机**，重插三次关了三次。原因：busybox crond 在关机时没来得及把任务标记为完成，下次开机发现"任务时间点已过"就**补跑**。

**防护：给 do_shutdown.py 加锁文件**（建议实施；当前仓库源码尚未包含）：

```python
LOCK = '/tmp/shutdown.lock'
# main() 开头：
if os.path.exists(LOCK):
    age = time.time() - os.path.getmtime(LOCK)
    if age < 600:                 # 10 分钟内重复执行直接跳过
        log('lock active (%.0fs), skip' % age)
        return
open(LOCK, 'w').write(str(time.time()))
```

锁放 tmpfs（/tmp）即可——重启后若时间已远离关机点，补跑也不会触发（可再叠加"仅在关机点 ±5 分钟内才执行"的时间窗判断，双保险）。

**已经中招的解救**：上电后立刻进系统（时间窗很短可先删 crontab 关机行）：

```sh
sed -i '/hwipi-sched-shutdown/d' /etc/crontabs/root
/etc/init.d/cron restart
```

### 定时开机

树莓派 4B 无 RTC / ACPI，**纯软件无法定时开机**。可行方案：DS3231 等 RTC 模块接 GPIO3 排针（支持闹钟唤醒），或智能插座定时上电。

## 2.20 Cloudflare Tunnel 公网访问（可选）

- cloudflared 必须走 **http2 over TCP 7844**（Cloudflare 隧道协议只在 7844 监听；443 是普通网站 HTTPS，不接受隧道协议，改走 443 会 TLS 报错）
- Public Hostname：`wifi.fmcoral.com` → `localhost:8080`
- **必须启用 Cloudflare Access 邮箱验证**，否则面板暴露公网
- 早期方案里的 cloudflared 自身隧道流量已在流量去向统计中排除

---

# 第三部分：踩坑大全

> 按类别汇总，每条含现象/原因/解法。

## 3.1 Windows / PowerShell / SSH

### 坑 1：CRLF 导致脚本无法执行
- 现象：`env: can't execute 'python3\r': No such file or directory`
- 解法：`tr -d '\r' < f.py > c.py && mv c.py f.py`

### 坑 2：`tr -d "\r"` 会删掉所有字母 r ★连环坑
- ash 双引号把 `\r` 当普通字符 r，曾把 shebang 删成 `#!/us/bin/env`
- **解法：必须单引号 `tr -d '\r'`**

### 坑 3：PowerShell 不支持 `&&` / heredoc
- 用 `;` 分隔；条件执行用 `if ($?) {...}`；多行 commit 信息用多个 `-m`

### 坑 4：`$?`、`<`、`()` 被 PowerShell 提前解析
- 远程命令外层用单引号，或让远端 shell 自己解析；`@{u}` 这类写法要加引号

### 坑 5：scp 必须 `-O`
- 树莓派 dropbear 无 sftp-server；多目标分开传

### 坑 6：SD 卡在 Windows 看不到配置
- 两个分区：FAT32 boot 可见，ext4 不可见。管理员 PowerShell：
  ```powershell
  wsl --mount "\\.\PHYSICALDRIVE1" --partition 2
  # WSL 内挂载改文件
  wsl --unmount "\\.\PHYSICALDRIVE1"
  ```

### 坑 7：SSH 报「BindInterface not supported on this platform」
- 多网卡 + `~/.ssh/config` 编码异常（UTF-16/BOM 被误解析成绑定指令）
- 解法：把 config 重写为纯 ASCII；用 `ssh -F NUL ...` 做对照验证
- 私钥有 passphrase 且无 agent 时，用 Python paramiko 密码登录更省事

## 3.2 认证逆向

| 坑 | 说明 / 解法 |
|----|------|
| Step9 401 | 字段名是动态的 `signedCimsResponse`，解析 postArgument |
| 加密明文 | `password\|token` 而非密码；padding PKCS1v1.5 |
| sig_request 拆分 | 以 `:` 分，前缀 XD\| / APP\| |
| iframe 会话 | Step5 不先访问，后续 API 无会话 |
| 蜜罐字段 | hidden username=aaaaaaa 原样回传，真账号在 API |
| 网关劫持形式 | **200 + body 内 JS 跳转**，不是 302；正则要从 body 提 eportal URL |
| eportal 强制 SSO | 无论怎么伪装请求头，eportal 都 302 到 SSO，"本地登录页绕过"不可行 |
| already online | 掉线误判时 eportal 返回已在线，脚本正确退出，不算失败 |
| 参数时效 | wlanuserip 绑定当前 IP/MAC，IP 一变必须重新触发 Step1 |
| 系统时间错误 | 无 RTC 时时间停留旧值；**实测不影响认证**，上线后 sysntpd 自动校准（曾偏差近 24h） |

## 3.3 DNS（2026-10-10 新增大类）

| 坑 | 说明 / 解法 |
|----|------|
| **/etc/hosts 死绑定** | 早期方案写死 `61.187.55.38 sso...`，dnsmasq 优先读 hosts、权重高于一切转发规则 → 必走 MITM 门。删除即可（备份 hosts.bak-mix） |
| **分裂 DNS** | 校园 DNS 给校内 10.8.9.35，公共 DNS 给公网 218.104.146.107，旧公网 61.187.55.38 只会来自 hosts。用谁的 DNS 决定进哪扇门 |
| **rebind 保护** | `rebind_protection=1` 默认丢弃"域名→内网 IP"应答；配 `rebind_domain='hunau.edu.cn'` |
| **uci 配置不落地** | uci add_list 的 server/address 没进生成的 dnsmasq.conf（ujail 启动的版本）；改写 /etc/dnsmasq.conf，并用"看生成文件"验证 |
| **gzip 压缩页** | 浏览器头导致 SSO 返回 gzip，urllib 不自动解压 → sig_request 正则失配。fetch 加 `_decode_body` 按 Content-Encoding 解压 |
| 排查顺序 | 配置怎么改都不生效时：① 看生成配置 ② 看 hosts ③ 直接问上游 `nslookup domain 10.10.10.10` ④ 抓原始响应头与前几字节（gzip 魔数 `\x1f\x8b`） |

## 3.4 掉线探测

| 坑 | 说明 / 解法 |
|----|------|
| 单 ping 误报 | 单个 ping 包丢包就触发认证（日志里 21/23 次是误报）→ 升级三目标，任一通即可 |
| ping 通≠在线 | 校园网放 ICMP 但 HTTP 劫持 → is_online 必须 ping + wget 百度不见 eportal 才算 |
| 探测目标选择 | 8.8.8.8 部分网不稳、119.29.29.29 实测不可达；定 223.5.5.5 / 180.76.76.76 / 8.8.8.8 |
| 误报留痕 | ping 误判触发、eportal 回 already online 的记录原样显示，便于观察 |

## 3.5 日志 / 存储 / cron

- `/var/log`、`/tmp` 是 tmpfs，重启清空 → 认证日志必须放 `/root/auto_auth.log`
- 日志要有大小上限，否则无限增长
- **crontabs 目录为空时 crond 启动即退出**（无报错）：先建 `/etc/crontabs/root` 再 start
- hotplug：脚本放 `/etc/hotplug.d/iface/`，变量 `$ACTION/$INTERFACE/$DEVICE`
- SD 卡磨损：每 1~2 分钟写几十 KB，正常可用 5 年以上；`/root` 写满时服务不崩但数据停更
- 所有落盘用原子写（tmp→replace），防断电半写损坏

## 3.6 nftables

| 坑 | 说明 |
|----|------|
| 纯 nft 系统 | 无 iptables |
| `route` 链不支持 | 用 filter 类型挂 postrouting |
| 花括号 | 整条规则引号包成一个参数 |
| `fwd` 是保留字 | 链名换掉，报错很隐晦 |
| 子命令静默失败 | 必须 `nft list table` 看实际内容，别只看返回码 |
| nlbwmon | 与 offload/6.12 内核不兼容，改用命名计数器 |
| 计数器负差 | 重启清零后差值为负 → 重置基线、跳过该轮，不污染累计 |

## 3.7 Python / I2C

| 坑 | 说明 |
|----|------|
| 硬编码偏移 | `up_`（3 字符）/`down_`（5 字符）统一 `[3:]` 产生脏键 → fromhex 抛错被 except 吞成静默死循环。按前缀分支；守护进程异常不能全吞 |
| `tr ... > f` 自截断 | 重定向先清空文件，必须输出到另一文件再 mv |
| I2C 假 ACK | 总线被拉低时扫任何地址都"有 ACK"；先确认空闲高电平 |
| 全黑排查顺序 | 引脚号 → 供电 → 9 时钟脉冲解锁 → 显存分段测试 |
| socket 挂属性 | 本环境 socket 对象不能加自定义属性，嗅探线程会启动即退 |
| 字节序笔误 | 每个数据包解析失败，最不起眼最难查 |
| bytes/str | 类型不统一会误判设备状态 |

## 3.8 无线

- sta+AP 共存必崩（2.15），别反复试
- BCM43455 不支持 WPA3/SAE，用 WPA2-PSK
- 改无线配置务必先接显示器键盘；radio 一崩只能拔 SD 卡
- 在线设备判定不能用 DHCP 租约文件（不准），用 `iw station dump`

## 3.9 电源 / 关机

| 坑 | 说明 |
|----|------|
| flag 文件缺失静默退出 | 早期版本靠 flag 控制启用，文件没建时脚本首行直接退出且无提示 → 已改为 crontab 行管理 |
| 缺执行权限 | `-rw-r--r--` 时 ash 报 Permission denied → chmod +x |
| 错过补跑连环关机 | 见 2.19；锁文件 + 时间窗双保险 |
| 关机必须善后 | 先落全部数据、OLED 显已关机、双 sync，再 poweroff |
| 无法软件定时开机 | 需 RTC 模块（GPIO3 闹钟）或智能插座 |

## 3.10 面板 / 图表

| 坑 | 说明 / 解法 |
|----|------|
| 速率贴地"平线" | 动态范围大，线性坐标被压扁 → 速率类统一**对数坐标** |
| Y 轴顶格 | 1-2-5 贴数据档位会导致图没填满 → 恢复整数量级取整 |
| X 轴末标签裁切 | 末点超 82% 位置时改 `text-anchor:end` |
| 两卡不等高 | 别用 align-items:start；恢复等高 + chartbox flex:1、svg height:100% |
| 假流量点 | 变量用错产生 367MB/s 坏点 → 1Gbps 线速钳制 |
| 5 秒台阶感 | 改 1s 采样 + 5s 滑动窗口平均 |
| ujail 隔离 | dnsmasq 日志在 ujail 里取不出 → DNS 数据改用 raw socket 嗅探 |
| 自身流量 | 统计要排除 cloudflared 隧道，否则混入设备账 |
| 浏览器缓存 | 面板大改后 Ctrl+F5 强刷验收 |
| 公网裸奔 | 面板映射公网必须套 Cloudflare Access |

---

# 第四部分：验证与运维

## 4.1 复刻完成验证清单

```sh
iw dev                          # AP 在广播：type AP ssid HWIFI
cat /tmp/dhcp.leases            # 终端拿到 192.168.1.x
nslookup sso.hunau.edu.cn 127.0.0.1 | grep 10.8.9.35   # DNS 走对门
wget -qO- http://www.baidu.com | grep -q eportal && echo OFF || echo ONLINE
pgrep -af crond                 # cron 在跑
nft list table ip ttlfix        # TTL 规则在
/etc/init.d/oled status; /etc/init.d/monitorweb status # 两个服务在跑
grep 're-auth SUCCESS' /root/auto_auth.log              # 自愈有成功记录
```

浏览器验收（面板改动后）：展开/图表/列表/时间切换/刷新保持/收起/空态/390px 手机端/控制台零报错。

## 4.2 日常运维

```sh
tail -30 /root/auto_auth.log          # 认证日志（最常用）
cat /tmp/dhcp.leases                  # 在线设备
ip route show                         # 路由
/root/auto_auth.py                    # 手动认证（在线静默退出）
cat /root/shutdown.log                # 关机记录
wifi reload                           # 重启 AP（约 20s）
```

## 4.3 故障排查

| 症状 | 排查方向 |
|------|---------|
| 连 HWIFI 上不了网 | auto_auth.log 是否 FAIL；ip route；ping 网关 |
| 列表没有 HWIFI | `iw dev` 看 AP；wifi reload |
| 终端拿不到 IP | AP 是否在 br-lan；dhcp.lan 是否 server |
| FAIL step1 | eportal 没劫持，可能其实已在线 |
| FAIL step3 status=200 | 历史上是 hosts 走错门；查 DNS 解析与 gzip |
| FAIL step4 no sig_request | 90% 是 gzip 未解压；少数是服务器页面变体，稍后重试 |
| FAIL step6/8 | SSO 可达性、账号状态、encryptor 是否 INTERNATIONAL |
| 开机即被关机 | 立即删 crontab 关机行（2.19） |
| radio 无反应 | 接显示器 wifi reload；不行拔 SD 卡改 disabled |

## 4.4 部署文件与运行时数据对照

| 仓库路径 | 部署位置 | 说明 |
|---------|---------|------|
| src/auto_auth.py | /root/auto_auth.py | 认证主程序 |
| src/auto_auth.conf.example | /root/auto_auth.conf | 账号模板（conf 不入库） |
| src/crontab-root | /etc/crontabs/root | cron |
| src/do_shutdown.py | /root/do_shutdown.py | 关机善后 |
| src/monitor_web.py | /root/monitor_web.py | 面板后端 |
| src/monitor.html | /root/monitor.html | 面板前端 |
| src/oled_status.py | /root/oled_status.py | OLED + 设备记账 |
| src/hotplug-routes.sh | /etc/hotplug.d/iface/99-routes | 路由持久化（wwan 分支已废弃） |
| src/firewall.ttl.sh | /etc/firewall.ttl.sh | TTL 伪装 |
| src/verify_ttl.py | /tmp/ | TTL 验证 |

运行时数据（均在 /root，断电最多丢一个写入周期）：auto_auth.log、shutdown.log、.oled_state、traffic_state.json、traffic_hist.json、device_seen.json、traffic_dest.json、temp30_hist.json、traffic_log/*.txt。

## 4.5 核心结论

1. **纯 Python 逆向 CIMS API 完全可行**，11 步链路稳定，掉线 1 分钟内自动恢复
2. **"SSO 被 MITM"是走错门的结果**：走校园 DNS → 校内 10.8.9.35 即与电脑弹窗同路，无需任何额外设备
3. **AP-only 热点稳定**，NAT 后校园网只见一个身份；sta+AP 并发受 BCM43455 固件所限不可用
4. **TTL 伪装**封堵最常用的防私接检测
5. **监控面板 + OLED + nft 计数器**构成完整可观测体系，数据持久化、跨重启不丢
6. 关机要做好善后并防 cron 补跑；定时开机需外部 RTC / 智能插座
7. 整套方案零额外硬件（面板/OLED 除外）、零人工干预、重启自恢复，适合同类锐捷 Portal + CIMS 高校参考
