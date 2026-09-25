#!/bin/sh
# TTL 伪装：eth0 出站包强制 TTL=64，防锐捷 TTL 跳数检测（防私接）
# 由 firewall include 调用，开机及 fw4 reload 时执行；幂等，可重复运行
# 注意：本内核不支持 route 类型链，用 filter 类型挂 postrouting

nft list table ip ttlfix >/dev/null 2>&1 || nft add table ip ttlfix

nft list chain ip ttlfix postrouting >/dev/null 2>&1 || \
  nft 'add chain ip ttlfix postrouting { type filter hook postrouting priority mangle; policy accept; }'

# 清空旧规则后重建（保证只有一条）
nft flush chain ip ttlfix postrouting
nft add rule ip ttlfix postrouting oifname eth0 ip ttl set 64
