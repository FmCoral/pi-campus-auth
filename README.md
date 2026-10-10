# Pi Campus WiFi Auto-Auth（HWIFI）

> 树莓派校园网全自动认证 + 软路由 + 实时监控面板：纯 Python 逆向 CIMS API，零额外硬件、零人工干预。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform: ImmortalWrt](https://img.shields.io/badge/ImmortalWrt-25.12.2-blue.svg)](docs/deployment-guide.md#13-硬件--软件环境)
[![Device: Raspberry Pi 4](https://img.shields.io/badge/Raspberry%20Pi-4B-C51A4A.svg)](docs/deployment-guide.md#13-硬件--软件环境)

---

## 简介

很多高校校园网采用「网页 Portal + SSO + 二次身份认证（CIMS）」，普通路由器无法完成认证。本项目在树莓派（ImmortalWrt）上用**纯 Python 逆向认证接口**，实现：

- 网口 `eth0` 接校园网，开机 / 掉线后**自动完成认证**（实测被挤下线 1 分钟内自愈）
- 板载 WiFi 广播热点 **HWIFI**，手机、电脑接入即可上网
- **NAT 单 IP** 出口 + **TTL 伪装**，规避校园网防私接检测
- **Web 监控面板**（8080 端口）：总览 / 终端设备 / 流量 / 温度 / 认证日志 / 电源管理
- 重启自恢复，全程不需要任何人工操作

适用于同类高校的「锐捷 ePortal + CAS + Trusfort CIMS」环境，逆向方法可推广到其他 Portal 认证。

## 特性

- 🔓 **纯脚本逆向**：不依赖无头浏览器（musl libc 跑不了 Chromium），仅用 Python 标准库 + openssl
- 🔁 **掉线自愈**：cron 每分钟探测（ping + HTTP 劫持双重判定），断网立即重认证
- 📶 **热点 AP**：5GHz VHT80，多终端接入，NAT 后校园网只见一个身份
- 🖥️ **监控面板**：实时速率（1 秒采样 + 5 秒滑动窗口，对数坐标）、设备在线时长、流量去向环形图、温度趋势、认证日志、电源管理
- 📺 **OLED 状态屏（可选）**：SSD1306 屏轮换显示连接数、实时速率、累计流量；nftables 按设备流量记账，重启不丢 + 按天落日志
- 🛡️ **防检测**：TTL 强制 64，封堵最常用的 TTL 跳数识别
- 🔌 **电源管理**：每日 23:20 定时关机 + 密钥保护的立即关机，关机前自动数据善后
- 🔐 **凭据分离**：账号密码走独立配置文件 / 环境变量，仓库内零敏感信息

## 网络拓扑

```
[校园网墙插] ──网线── [Pi eth0 / WAN]  DHCP 动态获取（示例 10.101.0.25/20）
                       │  默认路由；自动认证
                       [Pi br-lan]      192.168.1.1/24 (DHCP .100~.249)
                       ▲
                       [Pi phy0-ap0]    5GHz ch149 VHT80
                       SSID=HWIFI  WPA2-PSK
                       ▲
        ┌──────────────┼──────────────┐
   [手机]          [电脑]        [其他终端]     ← 经 NAT 上网
```

## 监控面板

浏览器访问 `http://192.168.1.1:8080`（**仅 LAN 侧可访问**，校园网侧被防火墙隔离）：

| 页面 | 内容 |
|------|------|
| 总览 | 实时上/下行速率（1s 采样 + 5s 滑动窗，对数坐标）、5 分钟实时趋势、KPI 卡片 |
| 终端设备 | 设备 KPI、在线状态副行（连接时长 / 最后在线）、累计在线时长、点击展开**流量去向**环形图（微信/QQ/抖音/B站等服务商占比，今日 / 近 7 天） |
| 流量 | 分钟级流量（3 天环形缓冲）、按天流量（30 天），均为对数坐标，1Gbps 异常值钳制 |
| 系统状态 | 分核 CPU / 内存明细 / 存储 / 接口 / 关键进程、温度趋势（1h / 24h / 3d 分段，65℃ 告警线）、**电源管理卡片** |
| 认证日志 | 按时间倒序，SUCCESS / FAIL 过滤，「立即重新认证」按钮 |

公网访问方案：Cloudflare Tunnel 映射至 `wifi.fmcoral.com`（隧道走 TCP 7844），并启用 Cloudflare Access 邮箱验证。

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

**5. 启用热点与监控面板**：按文档配置 `/etc/config/wireless`（AP 启用、sta 禁用），部署 monitor_web / OLED 服务，终端连接 HWIFI 即可上网。

> 完整步骤、配置文件原文、验证清单、全部踩坑记录见 **[部署文档](docs/deployment-guide.md)**。

## 目录结构

```
.
├── README.md                    # 本文件
├── LICENSE                      # MIT
├── .gitignore
├── src/                         # 生产代码（部署到树莓派）
│   ├── auto_auth.py             # 认证主程序（11 步链路 + gzip 自动解压）
│   ├── auto_auth.conf.example   # 账号配置模板（复制为 auto_auth.conf）
│   ├── crontab-root             # cron 配置
│   ├── do_shutdown.py           # 关机善后（停服务落盘→OLED显已关机→sync→poweroff）
│   ├── monitor_web.py           # 监控面板后端（单文件 HTTP 服务）
│   ├── monitor.html             # 监控面板前端（单文件）
│   ├── oled_status.py           # OLED 状态屏 + nftables 按设备流量记账守护进程
│   ├── hotplug-routes.sh        # 路由持久化（hotplug）
│   ├── firewall.ttl.sh          # TTL 伪装（nftables）
│   └── verify_ttl.py            # TTL 伪装验证脚本
├── tools/                       # 逆向/字模工具（不部署）
│   ├── gen_cjk_font.py          # OLED 中文/英文点阵字模生成
│   ├── preview_pages.py
│   ├── probe.py / probe2.py / probe3.py
│   ├── probe_logout.py
│   └── test_eth0_sso.py
├── docs/
│   ├── deployment-guide.md      # ★ 完整部署手册（含踩坑大全）
│   └── stage-summary.md         # 早期阶段性总结（历史归档）
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

三个最容易踩的坑（详见部署文档第五部分）：
1. POST 字段名不是 `sig_response`，而是页面动态配置的 `signedCimsResponse`
2. RSA 明文是 `password|token`，不是密码本身
3. SSO 返回 **gzip 压缩页**，urllib 不自动解压；且必须经校园 DNS 解析到校内 IP，`/etc/hosts` 写死公网 IP 会被网关 MITM

## 数据与持久化

| 文件（树莓派） | 内容 | 落盘节奏 |
|---|---|---|
| `/root/auto_auth.log` | 认证日志（cron 输出） | 实时追加，超 200KB 自动清空 |
| `/root/shutdown.log` | 关机执行记录 | 每次关机追加 |
| `/root/.oled_state` | 整机累计流量 | 每 10 分钟原子写 |
| `/root/traffic_state.json` | 设备累计流量（按 MAC） | 每 10 分钟原子写 |
| `/root/traffic_hist.json` | 分钟级流量（3 天环形） | 每 2 分钟原子写 |
| `/root/device_seen.json` | 设备最后在线 + 累计在线时长 | 在线每 60s，离线立即固化 |
| `/root/traffic_dest.json` | 流量去向（30 天） | 按天累计 |
| `/root/temp30_hist.json` | 温度半小时点（3 天 144 点） | 桶闭合即落盘 |
| `/root/traffic_log/*.txt` | 按天流量快照 | 每 10 分钟 |

所有落盘均采用「tmp → replace」原子写，断电最多丢失一个写入周期；加载函数全部 try/except，文件损坏自动从空值启动。

## 常见问题

- **板载 WiFi 能同时连热点（sta）又做 AP 吗？**
  BCM43455 固件下 sta+AP 并发会崩溃，已穷尽测试放弃，采用 AP-only。如需 sta，请加 USB WiFi 棒（独立 phy）。
- **会被校园网检测到吗？**
  NAT 保证单 IP/MAC/账号，TTL 伪装消除转发跳数特征，常规检测无法识别。仍建议终端数量适度、避免异常 P2P 流量。
- **认证失效怎么办？**
  cron 会自动重认证；日志在 `/root/auto_auth.log`，查 `re-auth SUCCESS` 即可确认。面板也提供「立即重新认证」按钮。
- **为什么 ping 通却上不了网？**
  校园网放通 ICMP 但劫持 HTTP。`is_online()` 已要求 ping + wget 百度不被 eportal 重定向才算在线。
- **怎么看每个设备用了多少流量？**
  设备页 / OLED 屏可看；完整数据在 `/root/traffic_state.json`，按天日志在 `/root/traffic_log/`。基于 nftables 计数器，需关闭 flow_offloading（见部署文档）。
- **定时关机后重插电会不会被连环关机？**
  存在 cron「错过补跑」风险（poweroff 导致 crond 未标记任务完成）。锁文件防护已在部署文档中给出方案；若再次遇到，进系统删掉 crontab 关机行即可。

## 免责声明

本项目仅供学习网络协议与个人合法接入使用，请遵守所在学校网络管理规定，勿用于账号共享、规避收费等违规用途。使用者自行承担一切责任。

## License

[MIT](LICENSE) © 2026 coral
