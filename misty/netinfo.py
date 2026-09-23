"""Misty POS - 接続画面に出す自アドレスの決定

QR に載せるのは「他の端末がこのサーバーに辿り着けるアドレス」でなければならない。

- request.host は使わない。ホスト機自身が http://127.0.0.1:5000/ で接続画面を開くと
  127.0.0.1 が QR に埋め込まれ、他の端末からは繋がらない。
- 「8.8.8.8 へのルートに使うNIC」だけで決めるのも不十分。Windows のモバイル
  ホットスポットは別のネットワーク（大学の Wi-Fi 等）の接続を共有する仕組みなので、
  ホスト機は上流とホットスポット側の2つのアドレスを持つ。既定ルートは上流側を指すが、
  レジやバーテンの端末が居るのはホットスポット側（Windows の ICS は 192.168.137.0/24）。

そこで全 IPv4 アドレスを集め、ホットスポット側 → その他プライベート → 既定ルート側
の優先度で選ぶ。候補は画面にも全部出し、外れたときに人が選べるようにしている。
"""
from __future__ import annotations

import ipaddress
import socket

# Windows のインターネット接続共有（モバイルホットスポット）が使う既定サブネット
WINDOWS_ICS_NET = ipaddress.ip_network("192.168.137.0/24")
# macOS のインターネット共有の既定サブネット
MACOS_SHARING_NET = ipaddress.ip_network("192.168.2.0/24")


def _route_ip() -> str | None:
    """既定ルートに使われるローカルアドレス。UDP の connect は経路解決だけでパケットは送らない。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.0)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _host_ips() -> list[str]:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return [info[4][0] for info in infos]


def _rank(ip: str, route_ip: str | None) -> tuple:
    addr = ipaddress.ip_address(ip)
    if addr in WINDOWS_ICS_NET or addr in MACOS_SHARING_NET:
        tier = 0
    elif addr.is_private and not addr.is_loopback and not addr.is_link_local:
        tier = 1
    elif ip == route_ip:
        tier = 2
    else:
        tier = 3
    # 同じ層の中では既定ルート側を優先し、それ以外は安定した順序にする
    return (tier, ip != route_ip, ip)


def candidate_ips(host_ips=None, route_ip=None) -> list[str]:
    """到達可能性が高い順のアドレス候補。引数はテストで差し替えるためのもの。"""
    route_ip = _route_ip() if route_ip is None else route_ip
    ips = set(_host_ips() if host_ips is None else host_ips)
    if route_ip:
        ips.add(route_ip)
    usable = []
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
            continue
        usable.append(ip)
    return sorted(usable, key=lambda ip: _rank(ip, route_ip))


def advertised_host(override: str = "") -> tuple[str, list[str]]:
    """(QR に使うアドレス, 候補一覧)。候補が1つもなければ 127.0.0.1 を返す。"""
    candidates = candidate_ips()
    if override:
        return override, candidates
    return (candidates[0] if candidates else "127.0.0.1"), candidates
