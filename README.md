# Pi Campus WiFi Auto-Auth（HWIFI）

> 树莓派校园网全自动认证 + 热点广播方案：纯 Python 逆向 CIMS API，零人工干预、零额外硬件。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform: ImmortalWrt](https://img.shields.io/badge/ImmortalWrt-25.12.2-blue.svg)](#环境)
[![Device: Raspberry Pi 4](https://img.shields.io/badge/Raspberry%20Pi-4B-C51A4A.svg)](#环境)

---

## 简介

很多高校校园网采用「网页 Portal + SSO + 二次身份认证（CIMS）」，普通路由器无法完成认证。本项目在树莓派（ImmortalWrt）上用**纯 Python 逆向认证接口**，实现：

- 网口 `eth0` 接校园网，开机 / 掉线后**自动完成认证**（3~10 秒恢复）
- 板载 WiFi 广播热点 **HWIFI**，手机、电脑接入即可上网
- **NAT 单 IP** 出口 + **TTL 伪装**，规避校园网防私接检测
- 重启自恢复，全程不需要任何人工操作

适用于同类高校的「锐捷 ePortal + CAS + Trusfort CIMS」环境，逆向方法可推广到其他 Portal 认证。

## 特性

- 🔓 **纯脚本逆向**：不依赖无头浏览器（musl libc 跑不了 Chromium），仅用 Python 标准库 + openssl
- 🔁 **掉线自愈**：cron 每分钟探测，断网立即重认证
- 📶 **热点 AP**：5GHz VHT80，多终端接入，NAT 后校园网只见一个身份
- 🛡️ **防检测**：TTL 强制 64，封堵最常用的 TTL 跳数识别
- 📺 **OLED 状态屏（可选）**：SSD1306 屏三页轮换显示连接数、实时速率、累计流量、系统状态；nftables 按设备流量记账，重启不丢 + 按天落日志
- 🔐 **凭据分离**：账号密码走独立配置文件 / 环境变量，仓库内零敏感信息

## 网络拓扑

```
[校园网墙插] ──网线── [Pi eth0 / WAN]  10.101.0.16/20
                       │  默认路由；自动认证
                       [Pi br-lan]      192.168.1.1/24 (DHCP)
                       ▲
                       [Pi phy0-ap0]    5GHz ch149 VHT80
                       SSID=HWIFI  WPA2-PSK
                       ▲
        ┌──────────────┼──────────────┐
   [手机]          [电脑]        [其他终端]     ← 经 NAT 上网
```

## 快速开始

**1. 烧录系统**：ImmortalWrt 25.12.2（bcm27xx/bcm2711），SD 卡启动。

**2. 配置网络**：把 eth0 设为 WAN（DHCP），AP 桥接 br-lan，防火墙放行 lan→wan 并开启 masq。详见部署文档。

**3. 部署认证脚本**：

```sh
# 上传 src/ 下文件到树莓派，然后：
tr -d '\r' < /root/auto_auth.py > /tmp/c.py && mv /tmp/c.py /root/auto_auth.py
chmod +x /root/auto_auth.py
cp /root/auto_auth.conf.example /root/auto_auth.conf
vi /root/auto_auth.conf          # 填入学号、密码
/root/auto_auth.py               # 手动跑一次验证
```

**4. 配置 cron（每分钟自愈）**：把 [src/crontab-root](src/crontab-root) 放到 `/etc/crontabs/root`，启动 crond。

**5. 启用热点**：按文档配置 `/etc/config/wireless`（AP 启用、sta 禁用），终端连接 HWIFI 即可上网。

> 完整步骤、配置文件原文、验证清单见 **[部署文档](docs/deployment-guide.md)**。

## 目录结构

```
.
├── README.md                    # 本文件
├── LICENSE                      # MIT
├── .gitignore
├── src/                         # 生产代码（部署到树莓派）
│   ├── auto_auth.py             # 认证主程序（11 步链路）
│   ├── auto_auth.conf.example   # 账号配置模板（复制为 auto_auth.conf）
│   ├── crontab-root             # cron 配置
│   ├── hotplug-routes.sh        # 路由持久化（hotplug）
│   ├── firewall.ttl.sh          # TTL 伪装（nftables）
│   ├── oled_status.py           # OLED 状态屏 + nftables 按设备流量记账守护进程
│   └── verify_ttl.py            # TTL 伪装验证脚本
├── tools/                       # 逆向调试脚本（不部署）
│   ├── probe.py / probe2.py / probe3.py
│   ├── probe_logout.py
│   └── test_eth0_sso.py
├── docs/
│   ├── deployment-guide.md      # ★ 完整部署手册（含踩坑大全）
│   └── stage-summary.md         # 早期阶段性总结
└── reference/                   # 逆向证据：抓取的前端 JS 与页面
    ├── CIMS.js / cims_web.js / cims_encryptor.js / jsencrypt.js
    ├── post.html / login.html / cims_login.html
    └── cas.txt / cims.txt
```

## 认证链路（逆向概要）

```
eportal redirect → sso authorize → cas/login
   → CIMS othertype（拿 RSA 公钥 + token）
   → RSA 加密 password|token
   → checkAuthcode（拿 usersign）
   → POST cas/login（拿 ticket → code）
   → eportal callback（完成认证）
```

两个最容易踩的坑（详见文档）：
1. POST 字段名不是 `sig_response`，而是页面动态配置的 `signedCimsResponse`
2. RSA 明文是 `password|token`，不是密码本身

## 常见问题

- **板载 WiFi 能同时连热点（sta）又做 AP 吗？**
  BCM43455 固件下 sta+AP 并发会崩溃，已实测放弃，采用 AP-only。如需 sta，请加 USB WiFi 棒。
- **会被校园网检测到吗？**
  NAT 保证单 IP/MAC/账号，TTL 伪装消除转发跳数特征，常规检测无法识别。仍建议终端数量适度、避免异常 P2P 流量。
- **认证失效怎么办？**
  cron 会自动重认证；日志在 `/root/auto_auth.log`，查 `re-auth SUCCESS` 即可确认。
- **怎么看每个设备用了多少流量？**
  OLED 屏轮播各设备累计流量；完整数据在 `/root/traffic_state.json`，按天日志在 `/root/traffic_log/`。基于 nftables 计数器实现，需关闭 flow_offloading（见部署文档 2.17）。

## 免责声明

本项目仅供学习网络协议与个人合法接入使用，请遵守所在学校网络管理规定，勿用于账号共享、规避收费等违规用途。使用者自行承担一切责任。

## License

[MIT](LICENSE) © 2026 coral
