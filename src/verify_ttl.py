#!/usr/bin/env python3
# 验证 TTL 伪装：用原始 socket 发 TTL=1 的 ICMP
# 无伪装：包到网关即死，返回 ICMP type11 (TTL exceeded)
# 有伪装：POSTROUTING 改写成 64，返回 type0 (echo reply)
import socket, struct, time, select

DST = '223.5.5.5'
s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
s.setsockopt(socket.SOL_IP, socket.IP_TTL, 1)
# ICMP echo: type8 code0 checksum id seq
pkt = struct.pack('!BBHHH', 8, 0, 0, 0x1234, 1) + b'ttl-verify-test'
def cksum(d):
    if len(d) % 2: d += b'\x00'
    s2 = sum(struct.unpack('!%dH' % (len(d)//2), d))
    s2 = (s2 >> 16) + (s2 & 0xffff); s2 += s2 >> 16
    return (~s2) & 0xffff
cs = cksum(pkt)
pkt = struct.pack('!BBHHH', 8, 0, cs, 0x1234, 1) + b'ttl-verify-test'
s.sendto(pkt, (DST, 0))
print('sent ICMP echo with TTL=1 to', DST)

deadline = time.time() + 5
while time.time() < deadline:
    r, _, _ = select.select([s], [], [], 2)
    if not r: break
    data, src = s.recvfrom(1024)
    ihl = (data[0] & 0xf) * 4
    t = data[ihl]
    print(f'reply from {src[0]}: ICMP type={t}', end='')
    if t == 0:
        print(' -> ECHO REPLY (TTL 伪装生效！包被改写成64到达目标)')
    elif t == 11:
        print(' -> TTL EXCEEDED (包以 TTL=1 死在半路，伪装未生效)')
    else:
        print()
