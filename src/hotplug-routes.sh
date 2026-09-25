#!/bin/sh
# 持久化校园网认证所需路由
# 部署位置：/etc/hotplug.d/iface/99-routes
# 触发：接口 ifup 时自动设置对应路由
#   - wan ifup  → 默认路由走 eth0（校园网出口）
#   - wwan ifup → sso(61.187.55.38) 走 phy0-sta0（coral 热点，绕过校园网 MITM）

[ "$ACTION" = ifup ] || exit 0

case "$INTERFACE" in
    wan)
        # 等接口完全 ready 再设路由
        sleep 1
        ip route replace default via 10.101.0.1 dev eth0 metric 0
        logger -t routes "default route via eth0 (wan ifup)"
        ;;
    wwan)
        sleep 1
        ip route replace 61.187.55.38/32 via 172.20.10.1 dev phy0-sta0 metric 0
        logger -t routes "sso route via phy0-sta0 (wwan ifup)"
        ;;
esac
