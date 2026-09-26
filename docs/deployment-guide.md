# 树莓派校园网自动认证 + 热点 HWIFI 完全部署手册

> 面向后来者的可复刻文档。记录从零搭建的完整步骤、逆向细节与所有踩过的坑。
> 完成时间：2026-09-25 ｜ 环境：湖南农业大学校园网（锐捷 ePortal + CAS + Trusfort CIMS）

---

# 第一部分：项目概述

## 1.1 目标

用一台树莓派实现：

1. **网口接校园网**，开机自动完成网页认证（锐捷 ePortal + SSO CAS + CIMS 二次认证）
2. **板载 WiFi 广播热点 HWIFI**，手机/电脑等终端接入即可上网
3. **掉线自动重认证**，全程零人工干预，不增加额外硬件
4. **规避校园网"防私接/防代理"检测**

## 1.2 最终网络拓扑

```
[校园网墙插] ──网线── [Pi eth0 / WAN]  10.101.0.16/20  gw 10.101.0.1
                       │   默认路由走这里；自动认证
                       │
                       [Pi br-lan]      192.168.1.1/24（DHCP 服务）
                       ▲
                       [Pi phy0-ap0]    5GHz channel 149 VHT80
                       SSID=HWIFI  加密=WPA2-PSK  密码=自定义
                       ▲
        ┌──────────────┼──────────────┐
   [vivo 手机]     [iPhone]       [电脑]        ← 终端经 NAT 上网
   192.168.1.106   .150           .140
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
| 控制电脑 | Windows 10/11 + PowerShell，另装 WSL2 Ubuntu-22.04（救砖用） |

### 校园网关键地址

| 名称 | 地址 | 说明 |
|------|------|------|
| ePortal 服务器 | `http://10.100.0.12` | 锐捷认证网关（内网） |
| SSO/CAS | `sso.hunau.edu.cn` = `61.187.55.38` | 学校统一认证（公网 v4） |
| 校园 DNS | `10.10.10.10` | **对 sso 域名只返回 v6**（坑） |
| 探测 IP | `8.8.8.8` / `223.5.5.5` | 掉线检测用 |

---

# 第二部分：从零复刻

## 2.1 烧录系统

1. 到 ImmortalWrt 官网下载 **bcm27xx/bcm2711** 的 factory 镜像（`.img.gz`），解压出 `.img`
2. 用 Rufus（选 DD 模式）或 balenaEtcher 烧入 SD 卡
3. 插卡上电。首次启动后默认 LAN 地址 `192.168.1.1`
   - 调试期可用网线把电脑接到树莓派，或直接接显示器+键盘（root 默认空密码）

> ⚠️ 树莓派 4B 默认 eth0 在 LAN 桥上。我们要把 eth0 改造成 WAN，见 2.2。

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

# wwan（连手机热点）在 AP-only 终态中不再使用，保留配置但 disabled
config interface 'wwan'
        option proto 'dhcp'
```

用命令行方式配置（uci）：

```sh
# eth0 从默认 LAN 桥移出、新建 WAN（uci commit network 后生效）
uci set network.wan=interface
uci set network.wan.proto='dhcp'
uci set network.wan.device='eth0'
uci commit network
```

## 2.3 防火墙 /etc/config/firewall

关键三个部分（其余默认规则保留）：

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
        option input 'REJECT'            # 注意：eth0 上 SSH 会被拦
        option forward 'DROP'
        option masq '1'                  # ★ NAT：终端用树莓派 IP 出去
        option mtu_fix '1'

config forwarding
        option src 'lan'
        option dest 'wan'                # ★ 允许终端转发到校园网
```

## 2.4 DHCP 服务（给热点终端分配 IP）

`/etc/config/dhcp` 中 lan 段默认已存在，确认如下：

```sh
config dhcp 'lan'
        option interface 'lan'
        option start '100'               # 192.168.1.100 起
        option limit '150'               # 到 192.168.1.249
        option leasetime '12h'
        option dhcpv4 'server'
```

无需改动，dnsmasq 默认随系统启动。

---

## 2.5 认证系统分析（逆向前必读）

该校认证是三段式：

```
锐捷 ePortal（网关）  →  CAS OAuth2.0（发授权码）  →  Trusfort CIMS（二次身份认证）
```

**关键认知**：普通的"账号密码 POST"拿不到登录态——CAS 登录页嵌了一个 CIMS iframe，密码要在 iframe 里经 RSA 加密、走 CIMS 独立 API 换成一个 `usersign`，再由父页面把 `usersign` 回填表单 POST 给 CAS。这就是为什么简单脚本登录会失败。

### 逆向方法论（如何自己分析）

1. 电脑接一个**能正常访问外网**的网络（手机热点），浏览器开 F12 → Network，勾选 Preserve log
2. 在未认证的网口上访问任意 `http://` 网站，观察完整跳转链
3. 右键保存登录页 HTML（本文环境里存成 `post.html`）
4. 从登录页里找到并下载三个关键 JS：
   - `CIMS.js`（父窗口控制逻辑）
   - `cims_web.js`（iframe 内 API 调用，即 CIMS-Web-Server-v3.js）
   - `cims_encryptor.js`（加密器选择）
5. 在 JS 里搜字段名、API 路径、encrypt 调用，逐步还原

---

## 2.6 认证链路完整逆向（11 步）

```
校园网关劫持 HTTP → eportal redirect
        │
 Step 1  GET http://1.1.1.1/
         返回一段 JS，正则提取：
         http://10.100.0.12/eportal/index.jsp?wlanuserip=<加密串>&...
         （wlanuserip 等是树莓派 IP/MAC 的加密参数，每次必须重新拿）
        │ 走 eth0
 Step 2  GET eportal index.jsp
         → 302 到 sso /oauth2.0/authorize（redirect_uri 含 wlanuserip）
        │ 走 sso 路由
 Step 3  GET sso authorize
         → 302 到 /cas/login?service=https://sso.../callbackAuthorize?...
        │
 Step 4  GET cas/login → 200 登录页，提取：
         • sig_request  格式  XD|<b64>|<hex>:APP|<b64>|<hex>
                        以 ':' 分成两半：
                        cims_sig = 左半（XD| 前缀）
                        app_sig  = 右半（APP| 前缀）
         • postArgument CIMS.init 里配置的 POST 字段名（★见 2.7）
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
         publicKey 是 X.509 SubjectPublicKeyInfo 的 base64 body
         拼成 PEM（加 -----BEGIN/END PUBLIC KEY-----，每行 64 字符）
         ★ 明文 = password + "|" + token（不是密码本身！）
         padding = PKCS1v1.5（JSEncrypt 标准）
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
 Step 11 GET eportal callback（上一步带 code 的 URL）
         eportal 标记该 IP 已认证 → eth0 可上网，ping 8.8.8.8 通
```

## 2.7 最致命的坑：POST 字段名（Step 9 的 401）

CIMS.js 的 `go()` 函数里，回填字段的名字**不是写死的**：

```js
input.name = options.postArgument;     // 字段名取自 CIMS.init 配置
input.value = response + ':' + appSig;
```

CIMS.js 默认值是 `sig_response`，**但本系统在登录页显式覆盖了**：

```js
CIMS.init({ ..., postArgument: 'signedCimsResponse', ... });
```

CAS 服务端只认 `signedCimsResponse`。用 `sig_response` 提交 → 字段缺失 → **401 Invalid credentials**。

**正确做法——动态解析，不要硬编码：**

```python
mpa = re.search(r'postArgument\s*:\s*[\'"]([^\'"]+)[\'"]', cas_body)
post_argument = mpa.group(1) if mpa else 'sig_response'   # 兜底默认值
postdata = {post_argument: sig_response}
```

> 排查 401 时曾怀疑 cookie / Referer / IP / URL 编码，全被证伪。
> 教训：**先核对前端 JS 的字段名，再怀疑网络层。**

## 2.8 前端 JS 逆向要点

| 文件 | 关键内容 |
|------|---------|
| CIMS.js | `parseSigRequest` 拆 sig；`onReceivedMessage` 收 iframe 消息；`go()` 回填并 submit；postArgument 默认值 line 97-99 |
| cims_web.js | `n()` = othertype 请求；`E()` = checkAuthcode，内部 `ao.encrypt(publicKey, password+"|"+token)`；成功后 `o()/d()` 用 postMessage 发 `GO|usersign` |
| cims_encryptor.js | encryptor=INTERNATIONAL → JSEncrypt（RSA PKCS1v1.5） |

postMessage 格式：`GO|AUTH|<base64>|<hex>`，父窗口去掉 `GO|` 后得到 usersign。

## 2.9 RSA 加密实现要点

树莓派没有 Python 加密库，直接调系统 openssl：

```python
def rsa_encrypt(public_key_b64, plaintext):
    pub = public_key_b64.replace('\n','').replace('\r','').replace(' ','')
    lines = [pub[i:i+64] for i in range(0, len(pub), 64)]
    pem = "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n"
    # 写临时文件 → openssl pkeyutl -encrypt -pkcs1 → base64
```

完整实现见 `auto_auth.py` 的 `rsa_encrypt()`。

## 2.10 关于 TGC 长效 Cookie（故意不复用）

Step 9 成功后 CAS 会发 `TGC=TGT-xxxx`。浏览器检测到 TGC 会跳过 CIMS 直接 SSO。

但脚本**故意每次新建 CookieJar、不复用 TGC**，因为：

- TGC 有服务端时效，失效后仍要走完整 CIMS
- 依赖它会产生"什么时候需要重新登录"的不确定性，违背零人工干预目标
- 每次跑完整 11 步链最稳健、可预测

---

## 2.11 部署生产脚本 auto_auth.py

把 [auto_auth.py](../src/auto_auth.py) 上传到树莓派 `/root/auto_auth.py`，结构：

| 函数 | 职责 |
|------|------|
| `load_credentials()` | 从 auto_auth.conf / 环境变量读取账号 |
| `is_online()` | ping 公网 IP，返回是否在线 |
| `rsa_encrypt()` | 调 openssl 做 RSA |
| `authenticate()` | 完整 11 步，返回 (bool, msg) |
| `main()` | 在线则静默退出；掉线则 authenticate，失败 exit(1) |

部署命令（注意行尾和权限，坑见第三部分）：

```sh
# 上传后必须做三件事：
tr -d '\r' < /root/auto_auth.py > /tmp/c.py && mv /tmp/c.py /root/auto_auth.py  # 去CRLF
chmod +x /root/auto_auth.py                                                     # 加执行权限
cp /root/auto_auth.conf.example /root/auto_auth.conf                            # 建账号配置
vi /root/auto_auth.conf                                                          # 填入真实学号密码
```

账号密码不再写死在脚本里，由 `/root/auto_auth.conf` 提供（该文件不入库，勿提交），也可用环境变量 `CAMPUS_USERNAME` / `CAMPUS_PASSWORD` 覆盖。

## 2.12 路由持久化（hotplug）

`/etc/hotplug.d/iface/99-routes`，接口 ifup 时自动设路由：

```sh
#!/bin/sh
[ "$ACTION" = ifup ] || exit 0
case "$INTERFACE" in
    wan)  sleep 1; ip route replace default via 10.101.0.1 dev eth0 metric 0 ;;
    wwan) sleep 1; ip route replace 61.187.55.38/32 via 172.20.10.1 dev phy0-sta0 ;;
esac
```

> AP-only 终态里 wwan 不触发，wan 分支负责保证默认路由。

另外必须加 hosts（否则校园 DNS 只给 v6，解析不了 sso）：

```sh
echo '61.187.55.38 sso.hunau.edu.cn' >> /etc/hosts
```

## 2.13 cron 部署（每分钟检测）

创建 `/etc/crontabs/root`：

```cron
* * * * * /root/auto_auth.py >> /root/auto_auth.log 2>&1
30 4 * * * [ $(stat -c%s /root/auto_auth.log 2>/dev/null || echo 0) -gt 200000 ] && echo > /root/auto_auth.log
```

第二条是日志超 200KB 自动清空（日志要放 /root 持久化，不能放 tmpfs，见 3.3）。

然后启动：

```sh
/etc/init.d/cron start      # 前提：crontabs 目录非空，见 3.4
/etc/init.d/cron enable     # 开机自启
```

---

## 2.14 部署热点 HWIFI

`/etc/config/wireless` 最终内容：

```sh
config wifi-device 'radio0'
        option type 'mac80211'
        option path 'platform/soc/fe300000.mmcnr/mmc_host/mmc1/mmc1:0001/mmc1:0001:1'
        option band '5g'
        option channel '149'          # ★ 固定信道（sta 曾用的信道）
        option htmode 'VHT80'
        option country 'CN'

config wifi-iface 'default_radio0'
        option device 'radio0'
        option network 'lan'
        option mode 'ap'
        option ssid 'HWIFI'
        option encryption 'psk2'
        option key '你的热点密码'
        option disabled '0'           # 启用 AP

config wifi-iface 'wwan'
        option device 'radio0'
        option network 'wwan'
        option mode 'sta'
        option ssid 'coral'
        option encryption 'psk2'
        option key '上级热点密码'
        option disabled '1'           # ★ 关掉 sta（见 2.15）
```

启用后 AP 自动加入 br-lan，终端即可连接。

## 2.15 ★ sta + AP 并发必须放弃（重要）

BCM43455 单 radio 的 sta 和 AP **单独使用都稳定，共存必崩**。虽然 `iw phy` 报告声称支持 `managed<=1, AP<=1`，但固件实际有问题。穷尽测试：

| 尝试方式 | 结果 |
|---------|------|
| `wifi reload`（sta+AP 同时起） | radio 崩，sta 不重连，SSH 断 |
| `wifi reconf`（sta 在线热加 AP） | 整个 radio 挂 |
| `wifi reconf`（AP 在线热加 sta） | AP+sta 全挂 |

dmesg 无 `firmware crashed`（接口能创建），但共存时互相干扰、sta 连上即被踢、hostapd 反复重启。

**结论：AP-only，sta 永久 disabled。** 若其他学校环境确需 sta，加一个 USB WiFi 棒（独立 phy）是唯一稳定解。

## 2.16 TTL 伪装防检测（防私接）

**原理**：终端发出的包 TTL=64，经树莓派 FORWARD 后变 63 再 SNAT 出去——网关一眼识别为"经过路由转发"，这是锐捷防私接最常用的检测。

**措施**：POSTROUTING 钩子把出站 TTL 强制改成 64。

先确认内核支持（在未挂钩的测试表试，无副作用）：

```sh
nft add table ip ttlprobe
nft add chain ip ttlprobe c
nft add rule ip ttlprobe c ip ttl set 64     # 不报错即支持
nft delete table ip ttlprobe
```

创建 `/etc/firewall.ttl.sh`（幂等）：

```sh
#!/bin/sh
nft list table ip ttlfix >/dev/null 2>&1 || nft add table ip ttlfix
nft list chain ip ttlfix postrouting >/dev/null 2>&1 || \
  nft 'add chain ip ttlfix postrouting { type filter hook postrouting priority mangle; policy accept; }'
nft flush chain ip ttlfix postrouting
nft add rule ip ttlfix postrouting oifname eth0 ip ttl set 64
```

注册成 firewall include（实现持久化）：

```sh
uci add firewall include
uci set firewall.@include[-1].type='script'
uci set firewall.@include[-1].path='/etc/firewall.ttl.sh'
uci set firewall.@include[-1].reload='1'
uci commit firewall
sh /etc/firewall.ttl.sh          # 立即生效，不断网
```

> 注：本内核不支持 `route` 类型链，必须用 `filter` 类型挂 postrouting（见 3.5）。

### TTL 效果验证方法

用原始 socket 发一个 **TTL=1** 的 ICMP：

- 无规则：包死在第一跳，无回应
- 有规则：出口被改成 64，收到目标 echo reply ← 证明伪装生效

验证脚本见 [verify_ttl.py](../src/verify_ttl.py)。

## 2.17 OLED 状态屏 + 按设备流量统计（可选扩展）

### 硬件与接线

0.96 寸 SSD1306 12864 I2C OLED（地址 0x3C），接树莓派排针：

| OLED | 树莓派物理引脚 | 说明 |
|------|--------------|------|
| VCC | 引脚 1 | 3.3V |
| GND | 引脚 9 | GND |
| SDA | 引脚 3 | GPIO2（硬件 I2C） |
| SCL | 引脚 5 | GPIO3 |

启用硬件 I2C（/boot/config.txt 加 `dtparam=i2c_arm=on`，ImmortalWrt 在 `/etc/config/system` 或 boot 分区配置），装 `python3` 即可，驱动纯标准库（fcntl + os.write /dev/i2c-1），无需 smbus。

> 注意：普通 12864 模块需 9 个 SCL 脉冲解锁被卡死的 I2C 总线；接线务必核对引脚号（插 1/3/5/9 排曾因看错排导致全线拉低假 ACK）。

### 屏幕显示（三页轮换，每页 1 秒，0.5 秒刷新率）

- 页1 网络：`L:` 接入终端数、`U:/D:` 实时上传/下载速度（0.5s 采样）、累计流量
- 页2 系统：`NET:` 校园网连通 T/F（ping 223.5.5.5）、CPU 温度、内存、CPU 占用
- 页3 设备：每台接入设备的 hostname + 累计流量（下载+上传）

### 按设备流量记账原理（nlbwmon 方案被否决）

先装了 nlbwmon，实测**与 flow offloading 不兼容**：开启软/硬分载后 conntrack 计数恒为 0，nlbwmon 永远空表；关闭 offload 后其 netlink 通道在该内核（6.12）仍不产数据，放弃。

最终方案 **nftables 命名计数器**，全自动对账：

1. OLED 守护进程每 5 秒读 `/tmp/dhcp.leases`，为每个租约 IP 在独立表 `inet hwacct` 建 `up_<IP十六进制>` / `down_<IP十六进制>` 计数器 + forward 钩子规则；租约消失则删除
2. 读计数器差值，按 **MAC** 累计（设备换 IP 不丢账），写 `/root/traffic_state.json`（含计数器基准值，重启不重复计数）
3. 每 10 分钟追加一行设备快照到 `/root/traffic_log/YYYY-MM-DD.txt`（按天分文件）

```sh
# 部署（脚本见 src/oled_status.py）
scp src/oled_status.py root@192.168.1.1:/tmp/
ssh root@192.168.1.1 "tr -d '\r' < /tmp/oled_status.py > /root/oled_status.py; chmod +x /root/oled_status.py"
# procd 服务（开机自启 + 崩溃 respawn），/etc/init.d/oled：
#   procd_set_param command /usr/bin/python3 /root/oled_status.py
#   procd_set_param respawn 3600 5 5
/etc/init.d/oled enable && /etc/init.d/oled start
```

**代价**：按设备计数要求流量走软件转发，必须保持 `flow_offloading=0`（见 2.16 前提）。树莓派 4B 软转发对 3~5 个终端的日常流量完全够用；若追求千兆线速可删除 OLED 的对账逻辑后重新打开 offload。

### 查看设备流量

```sh
cat /root/traffic_state.json                       # 实时累计（按 MAC）
cat /root/traffic_log/2026-09-26.txt               # 当天历史快照
nft list counters                                  # 原始计数器
```

> 应用级归属（"抖音用了多少"）受 HTTPS 加密限制只能按域名/SNI 近似推断，本项目不做。

---

# 第三部分：踩坑大全

## 3.1 Windows / PowerShell 类

### 坑 1：CRLF 行尾导致脚本无法执行
- 现象：`env: can't execute 'python3\r': No such file or directory`
- 原因：Windows 创建的文件是 CRLF，shebang `#!/usr/bin/env python3\r` 末尾 `\r` 被当成程序名
- 修复：`tr -d '\r' < f.py > c.py && mv c.py f.py`

### 坑 2：ash 双引号会把 `\r` 转义成字母 r ★连环坑
- 若写成 `tr -d "\r"`，ash 把 `\r` 解释成普通字符 `r`，于是**删掉文件里所有字母 r**
- 本文档曾把 shebang 删成 `#!/us/bin/env`，整个脚本报废
- **正确：单引号 `tr -d '\r'`**

### 坑 3：PowerShell 不支持 `&&`
- 用 `;` 分隔多条命令；需要判断成功再执行时写成 `if ($?) {...}`

### 坑 4：`$?` 在双引号里被 PowerShell 提前解析
- 远程命令里要取 Linux 的 `$?`，外层 SSH 参数用单引号包裹
- PowerShell 还保留 `<`、`>`、`(`、`)` 等字符，含这些的远程命令尽量让远端 shell 自己解析

### 坑 5：scp 必须用 `-O`，且多文件/多目标写法易错
- 树莓派无 sftp-server：`scp -O ...`
- 一次传多个文件到同一目标可以，但"多源多目标"不支持，分开传最稳

### 坑 6：SD 卡在 Windows 看不到配置文件
- SD 卡有两个分区：FAT32 boot（Windows 可见）+ Linux ext4（资源管理器不显示）
- 用 WSL2 挂载（需**管理员** PowerShell）：
  ```powershell
  wsl --mount "\\.\PHYSICALDRIVE1" --partition 2
  wsl -d Ubuntu-22.04            # 进去 mount /dev/sdX2 后改文件
  wsl --unmount "\\.\PHYSICALDRIVE1"
  ```

## 3.2 认证逆向类

| 坑 | 说明 |
|----|------|
| Step9 401 | 字段名应为 `signedCimsResponse` 而非 `sig_response`，动态解析 postArgument |
| 加密明文 | 是 `password\|token`，不是 password；padding 是 PKCS1v1.5 |
| sig_request 拆分 | 以 `:` 分 cims_sig / app_sig，两半前缀分别是 XD\| / APP\| |
| iframe 会话 | Step 5 必须先访问 iframe，否则后续 API 无会话 |
| 蜜罐字段 | hidden 的 username=aaaaaaa 要原样回传，真正账号在 API 里 |
| 无 curl/tcpdump | ImmortalWrt 默认只有 wget/uclient-fetch，抓包用 Python 写 |

## 3.3 日志 / 存储类

- `/var/log`、`/tmp` 都是 **tmpfs（内存盘），重启清空**
- 认证日志必须放 `/root/auto_auth.log`（overlay 持久层）
- 长期运行要加日志大小限制，否则无限增长

## 3.4 cron 类

### crontabs 目录为空时 crond 不启动
- `/etc/init.d/cron` 的 start_service：`[ -z "$(ls /etc/crontabs/)" ] && return 1`
- 现象：start 无报错但进程不存在
- 修复：**先建 `/etc/crontabs/root` 文件，再 start**

### hotplug 机制
- 脚本放 `/etc/hotplug.d/iface/`，变量：`$ACTION`、`$INTERFACE`、`$DEVICE`
- procd 以 source 方式调用，不强制要 +x（加上更稳）；文件在 /etc 持久层

## 3.5 nftables 类

| 坑 | 说明 |
|----|------|
| 系统是纯 nft | 无 iptables 命令，规则用 nft 写 |
| `route` 链不支持 | 报 "Chain of type route is not supported"，改用 **filter** 类型挂 postrouting |
| 花括号被本地 shell 吞 | 含 `{ }` 的整条 nft 规则用引号包成一个参数：`nft 'add chain ... { ... }'` |
| 独立表更稳 | TTL 规则放单独表 `ip ttlfix`，fw4 reload 时不会被清掉 |
| `fwd` 是链名保留字 | `nft add chain inet x fwd` 报 syntax error，换名字（如 `acct`）即可，报错很隐晦 |
| nft 子命令静默失败 | 脚本里 nft 报错只写 stderr，OLED 守护进程曾因链没建成而"看起来正常"；排查用 `nft list table` 看实际生效内容 |
| nlbwmon 与 offload 不兼容 | 开 flow_offloading 后 conntrack 字节恒 0，nlbwmon 永远空表；关掉 offload 后其 netlink 通道在 6.12 内核仍不产数据，最终改用自建命名计数器 |

## 3.7 Python / I2C 类

| 坑 | 说明 |
|----|------|
| `name[3:]` 式硬编码偏移 | `up_` 是 3 字符、`down_` 是 5 字符，统一 `[3:]` 会留下 `n_xxx` 脏键 → 后续 `fromhex()` 抛 ValueError → 被 main 的 except 吞掉变成静默死循环。教训：解析要按前缀分支；守护进程的异常不能全吞，要留 stderr |
| `tr -d '\r' < f > f` 自截断 | 重定向先清空文件，tr 读到空。必须输出到另一个文件再 mv |
| I2C 总线假 ACK | 总线被拉低时扫描任何地址都"有 ACK"；先看 `i2cdetect` 前置条件（总线空闲电平应为高）再信结果 |
| 屏幕全黑排查顺序 | ① 接线引脚号（看错排会全低电平）② 供电 ③ 9 时钟脉冲解锁总线 ④ 数据路径（强制全亮 vs 写显存分段测试） |

## 3.8 无线类

- sta+AP 并发必崩（见 2.15），别反复尝试浪费时间
- 树莓派 4B BCM43455 不支持 WPA3/SAE，连热点用 WPA2-PSK
- 调试无线务必**先接好显示器键盘**，否则 radio 一崩只能拔 SD 卡

---

# 第四部分：验证与运维

## 4.1 复刻完成验证清单

```sh
# 1. AP 在广播
iw dev                          # 应看到 type AP、ssid HWIFI（phy0-ap0）

# 2. 终端拿到 IP
cat /tmp/dhcp.leases            # 应列出已接入客户端及 192.168.1.x

# 3. 树莓派自身能上网
ping -c 2 8.8.8.8

# 4. 转发/NAT 生效：终端连上 HWIFI 后打开网页即可（人工验证）

# 5. cron 在跑
pgrep -af crond

# 6. TTL 伪装在跑
nft list table ip ttlfix        # 应有 ip ttl set 64 规则

# 7. 掉线自愈（真实会话失效后查日志）
grep 're-auth SUCCESS' /root/auto_auth.log
```

## 4.2 日常运维命令

```sh
# 看认证日志（最常用）
tail -30 /root/auto_auth.log

# 看接入的设备
cat /tmp/dhcp.leases

# 看当前路由
ip route show

# 手动触发一次认证（在线时静默退出）
/root/auto_auth.py

# 重启 WiFi（AP-only，约 20 秒恢复）
wifi reload
```

从电脑控制：

```powershell
ssh root@192.168.1.1 "tail -20 /root/auto_auth.log"
```

## 4.3 故障排查手册

| 症状 | 排查方向 |
|------|---------|
| 连着 HWIFI 但上不了网 | 先看 `/root/auto_auth.log` 是否 FAIL；再 `ip route`、ping 网关 |
| WiFi 列表没有 HWIFI | 控制台 `iw dev` 看 AP 是否存在；`wifi reload` |
| 终端获取不到 IP | 确认 AP 在 br-lan（`brctl show`）；dhcp.lan 是否 server |
| 认证一直 FAIL step1 | eportal 没劫持，可能其实在线；检查是否已能上网 |
| 认证 FAIL 在 step6/8 | sso 是否可达；账号状态；加密器是否仍 INTERNATIONAL |
| 整台 radio 没反应 | 接显示器 `wifi reload`；不行则 SD 卡改 disabled（见坑 6） |

## 4.4 保底方案（若 sso 无法走 eth0）

存在一个尚未验证的假设：**未认证状态下 sso 能否直接经 eth0 访问**（逻辑上应该可以——sso 是学校登录服务器，未认证时必须可达；本项目当前已让它走 eth0）。

若将来真实掉线后日志显示认证失败（卡在访问 sso），启用保底脚本：

- 每天凌晨（如 4:00）自动执行：关 AP → 启 sta（连热点）→ 跑认证 → 关 sta → 开 AP
- 全自动，仅凌晨断网 1~2 分钟
- 或更稳妥：购置 USB WiFi 棒做独立 sta（约 10-20 元）

## 4.5 仓库文件清单

| 仓库路径 | 部署位置 | 说明 |
|---------|---------|------|
| src/auto_auth.py | 树莓派 `/root/` | 认证主程序（生产版） |
| src/auto_auth.conf.example | 树莓派 `/root/auto_auth.conf` | 账号配置模板（复制后填真实信息） |
| src/crontab-root | 树莓派 `/etc/crontabs/root` | cron 配置 |
| src/hotplug-routes.sh | 树莓派 `/etc/hotplug.d/iface/99-routes` | 路由持久化 |
| src/firewall.ttl.sh | 树莓派 `/etc/firewall.ttl.sh` | TTL 伪装脚本 |
| src/verify_ttl.py | 树莓派 `/tmp/` | TTL 伪装验证脚本 |
| src/oled_status.py | 树莓派 `/root/oled_status.py` | OLED 状态屏守护进程 + 按设备流量记账 |
| tools/probe.py ~ probe3.py | 不部署 | 逆向调试版（11 步 + 诊断输出） |
| tools/probe_logout.py、test_eth0_sso.py | 不部署 | 下线/链路探测实验脚本 |
| reference/CIMS.js 等 JS | 不部署 | 逆向用前端源码（证据留存） |
| reference/post.html、login.html、cims_login.html | 不部署 | 抓取的认证页面（含 CIMS.init） |

运行后产生的 `auto_auth.log` 在树莓派 `/root/`（持久化）。

---

## 4.6 核心结论

1. **纯 Python 逆向 CIMS API 自动认证完全可行**，11 步链路稳定，真实掉线 3 秒内自动恢复
2. **AP-only 热点模式稳定可靠**，三终端实测正常上网，NAT 后校园网只见一个身份
3. **板载 WiFi 的 sta+AP 并发不可用**（BCM43455 固件缺陷），需要并发就上 USB WiFi 棒
4. **TTL 伪装**可封堵最常用的防私接检测
5. **OLED 状态屏 + nft 命名计数器按设备记账**：不依赖 nlbwmon（与 offload 不兼容），流量走软转发，状态持久化可跨重启
6. 整套方案零额外硬件（OLED 屏为可选扩展）、零人工干预、重启自恢复，适合同类高校锐捷/Portal + CIMS 环境参考
