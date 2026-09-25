# 树莓派软路由 - 阶段性总结

更新时间：2026-09-23

---

## 一、当前状态

| 项目 | 状态 |
|------|------|
| 系统启动 | ImmortalWrt 25.12.2，树莓派 4B（bcm2711, aarch64_cortex-a72），root 空密码 |
| WiFi 客户端（coral sta） | 已连，IP 172.20.10.2/28，作为 SSH 控制路径 |
| WAN（eth0 校园网） | 已认证上网，IP 10.101.0.16/20，网关 10.101.0.1 |
| 默认路由 | 走 eth0（校园网出口） |
| 经校园网 ping 8.8.8.8 | 通（~190ms） |
| HTTP 不再被劫持 | 是（认证成功） |

---

## 二、网络拓扑

```
[校园网墙插] ──网线── [Pi eth0 / WAN] 10.101.0.16/20  ←─ 默认路由, 已认证
                       [Pi br-lan]  192.168.1.1/24     ←─ LAN 桥（当前无成员）
                       [Pi phy0-sta0] 172.20.10.2/28   ←─ sta 连 coral 手机热点
[手机热点 coral] ──WiFi─↑                              ←─ SSH 控制路径（必须不掉）
[电脑 172.20.10.6] ──WiFi── coral ──→ SSH 到 Pi 172.20.10.2
```

**关键设计**：coral sta（phy0-sta0）是 SSH 控制路径，eth0 是数据路径。两条路径独立，配置 eth0/WAN 时不能动 wireless。

---

## 三、已完成配置（已 uci commit 持久化）

### 3.1 network

```
network.@device[0]   = device  name=br-lan type=bridge ports=''  ←─ eth0 已从桥中移出
network.lan          = interface device=br-lan proto=static ipaddr=192.168.1.1/24 ip6assign=60
network.wwan         = interface proto=dhcp                 ←─ 承载 coral sta
network.wan          = interface proto=dhcp device=eth0     ←─ 新建 WAN
network.wan6         = interface proto=dhcpv6 device=eth0 reqaddress=try reqprefix=auto  ←─ 校园网不给 v6，pending（无影响）
network.loopback     = interface proto=static ipaddr=127.0.0.1/8
```

### 3.2 wireless

```
radio0              type=mac80211 band=5g channel=auto htmode=VHT80 country=CN
default_radio0      (AP) disabled=1                       ←─ AP 暂关，最终目标要启用
wwan                wifi-iface device=radio0 network=wwan mode=sta
                    ssid=coral encryption=psk2 key=<热点密码> ieee80211w=1
```

注意：树莓派 4B BCM43455 brcmfmac 固件不支持 WPA3/SAE，sta 用 psk2（coral 是 PSK+SAE transition 模式）。

### 3.3 firewall

```
defaults   input=REJECT forward=REJECT
zone[lan]  name=lan network='lan wwan' input=ACCEPT forward=ACCEPT
zone[wan]  name=wan network='wan wan6' input=REJECT forward=DROP masq=1
forwarding lan->wan
```

### 3.4 dropbear（SSH）

```
Port=22 PasswordAuth=on RootPasswordAuth=on
（Interface 字段已删，监听 0.0.0.0:22）
```

---

## 四、校园网认证流程（已跑通，可复用）

**校园**：湖南农业大学，**认证系统**：锐捷 ePortal + sso CAS OAuth2 + Trusfort CIMS 二次认证

### 4.1 流程链

1. 树莓派访问任意 HTTP → 校园网关劫持 → 返回 ePortal redirect 脚本（带加密参数 wlanuserip/wlanacname/nasip/mac/t/url）
2. 访问 `http://10.100.0.12/eportal/login_sso.jsp?...` → 302 到 sso authorize
3. sso authorize → 302 到 cas/login?service=callbackAuthorize（带 CAS_SESSION cookie）
4. cas/login 显示登录页（用户名/密码 + CIMS 二维码 iframe）
5. 用户在校园认证 App 扫码 → CIMS 颁发 signedCimsResponse → JS 自动 POST 到 cas/login
6. CAS 验证 → 302 到 callbackAuthorize?ticket=ST-xxx
7. 浏览器跟随 → 302 到 redirect_uri?code=OC-xxxxx
8. **redirect_uri 是 `http://10.100.0.12:80/eportal/login_sso.jsp?...&code=OC-xxxxx`**
9. 树莓派经 eth0 访问该 URL → ePortal 返回 success.jsp → 认证完成

### 4.2 关键参数（绑定树莓派当前 IP/MAC，IP 变就要重拿）

```
wlanuserip=996fab4df67a46993b7327df6c9e3f07   ←─ 树莓派 IP 10.101.0.16 加密
wlanacname=51c3203c7f3e1b8b
ssid=
nasip=94eaca84664891726fca44e0aebf535e
mac=68d744437632f8666bdc5f3b517925d2         ←─ 树莓派 MAC 加密
t=wireless-v2
url=04406ae0360f47fc3741a2db1a40c9bd
```

### 4.3 sso 域名解析坑

- sso.hunau.edu.cn 公网有 v4(61.187.55.38) + v6(2408:8653:21::a001:7)
- **校园网内 DNS（10.10.10.10）只返回 v6**（校园网策略）
- 校园网关未认证前对 eth0 不广播 RA、不响应 DHCPv6 SOLICIT，**树莓派拿不到 v6**
- 必须用 v4 访问 sso，已加 `/etc/hosts`：`61.187.55.38 sso.hunau.edu.cn`
- 校园网关 MITM 所有 HTTP（包括访问 sso 的 80 端口）→ 未认证前 sso 必须用 https

### 4.4 已装的辅助包

```
libustream-openssl  ←─ 给 uclient-fetch 提供 TLS（mbedtls 默认对 ECDSA 弱密钥严格）
ca-bundle           ←─ /etc/ssl/certs/ca-certificates.crt 已就位
```

### 4.5 已知卡点

- CIMS 二次认证**必须人工扫码**，无法用脚本纯自动完成（除非有逆向 CIMS API 的方案）
- 浏览器拿 code 流程：电脑浏览器打开 sso authorize URL → 扫码 → 浏览器最后跳到 `http://10.100.0.12/...&code=OC-xxx` → 电脑访问不到（正常）→ 从地址栏复制 URL → 把 code 给树莓派 wget

---

## 五、最终目标拆解（暂未做）

> 目标：树莓派做软路由，连校园网，掉线自动重认证，其他终端连树莓派且不被风控

### 5.1 启用板载 WiFi AP（让其他终端连树莓派）

- `default_radio0` 当前 `disabled=1`，需启用并配置 SSID/密码
- AP 要桥接到 br-lan，让 DHCP/转发正常
- **风险**：radio0 同时跑 sta（5G VHT80）+ AP，需确认 brcmfmac 固件支持同频段并发（sta+AP 同 channel）
- 如不支持，需：
  - 方案 A：AP 用 2.4G（但树莓派 4B 单 radio，只能 5G 或 2.4G 之一）
  - 方案 B：AP 和 sta 同频段（5G），AP channel 跟 sta 关联的 AP
- 风控规避：AP 下挂的终端 NAT/masq 后用树莓派 eth0 IP 出去（已 masq=1），校园网只看到树莓派一个 MAC/IP

### 5.2 掉线自动重认证

- 校园网认证有时效（具体未测，估计几小时~一天）
- 需要：
  - 探测脚本：定时 ping 公网 IP（如 8.8.8.8），失败则触发重认证
  - 重认证脚本：拿新的 wlanuserip 加密串（重新访问 HTTP 触发 ePortal redirect 拿到）→ 走 sso + CIMS 流程拿 code → 访问 callback URL
  - **CIMS 二次认证的人工依赖**是最大障碍，需要：
    - 选项 1：复用之前的 CAS 长效 cookie/session（如果 CAS 支持 remember-me）
    - 选项 2：CIMS 提供"长期授权"或"设备绑定"机制
    - 选项 3：逆向 CIMS API，本地缓存 signedCimsResponse（短期有效）
    - 选项 4：sso 是否有"密码 grant"类型的 OAuth2 接口（直接换 token 不走 CIMS）
- 都需要进一步探测 sso 和 CIMS 的能力

### 5.3 默认路由持久化

- 当前 `ip route replace default via 10.101.0.1 dev eth0 metric 0` 是手动的，重启/reload 会丢
- 解决：删除 wwan 的默认路由获取（让 wwan 不抢默认路由），或者用 mwan3 多 WAN 负载均衡/备份
- 或：在 `/etc/hotplug.d/iface/` 加脚本，eth0 up 时设默认路由

### 5.4 风控规避

- eth0 masq 已开，下挂终端经树莓派 NAT 出去，校园网只看到树莓派 MAC/IP
- 但树莓派 MAC（88:a2:9e:48:3d:3e）+ 学号绑定关系已建立，如果校园网检测异常流量（多设备 NAT、P2P）会风控
- 需关注：
  - 流量类型（避免 P2P/大流量）
  - TTL 是否被改（默认 OpenWrt 不动 TTL，OK）
  - DHCP fingerprint（树莓派只一个 MAC 出现，OK）

---

## 六、关键命令参考

### 6.1 SSH 到树莓派（经 coral）

```bash
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=nul -o BatchMode=yes root@172.20.10.2 "命令"
```

### 6.2 查看当前网络状态

```bash
ip route                       # 默认路由
ip -br a                       # 接口地址
ifstatus wan                   # WAN 状态
ifstatus wwan                  # coral sta 状态
```

### 6.3 重新拿 ePortal 加密参数（IP 变化时）

```bash
# 在树莓派上：
wget -O - http://1.1.1.1/   # 看返回脚本里的 wlanuserip=xxx&...&url=xxx
```

### 6.4 sso authorize 完整 URL 模板

```
https://sso.hunau.edu.cn/cas/oauth2.0/authorize?response_type=code&client_id=xyw202307&redirect_uri=http%3A%2F%2F10.100.0.12%3A80%2Feportal%2Flogin_sso.jsp%3Fwlanuserip%3D{wlanuserip}%26wlanacname%3D{wlanacname}%26ssid%3D%26nasip%3D{nasip}%26mac%3D{mac}%26t%3Dwireless-v2%26url%3D{url}
```

`{...}` 占位符从 ePortal redirect 脚本里提取。

### 6.5 用 code 完成 ePortal 认证

```bash
# 在树莓派上，确保默认路由走 eth0：
ip route replace default via 10.101.0.1 dev eth0 metric 0
wget -O - 'http://10.100.0.12/eportal/login_sso.jsp?wlanuserip=...&code=OC-xxxxx'
# 看到 success.jsp 即成功
```

### 6.6 临时切默认路由到 coral（联网装包用）

```bash
ip route replace default via 172.20.10.1 dev phy0-sta0 metric 0
# 用完恢复：
ip route replace default via 10.101.0.1 dev eth0 metric 0
```

---

## 七、待办列表（按优先级）

- [ ] 启用 radio0 AP，让其他终端能连树莓派（最终目标核心）
- [ ] 验证 brcmfmac 是否支持 sta+AP 同频段并发
- [ ] 写掉线检测+自动重认证脚本（CIMS 自动化是最大障碍，需先探测 sso 长效 session 可能性）
- [ ] 默认路由持久化（hotplug 脚本或 mwan3）
- [ ] root 密码收紧（当前空密码，临时态）
- [ ] dropbear 仅监听 lan zone（不要暴露到 wan）
- [ ] 备份配置：`sysupgrade -b` 生成 backup

---

## 八、风险与注意事项

1. **coral sta 不能掉**：所有 wireless 配置变更前要先验证 SSH 经 coral 可达，配置失败时靠 coral 回退
2. **eth0 IP 变化要重拿 code**：wlanuserip 加密串绑定 IP，DHCP 续约换了 IP 就要重新走 sso 流程
3. **CIMS 二次认证时效**：每次重认证都要人工扫码，除非找到自动化方案
4. **校园网风控**：避免明显异常流量，AP 下挂数量不要太多
5. **配置变更前先 uci show 备份**：`uci show > /tmp/uci-backup-$(date +%s).txt`
