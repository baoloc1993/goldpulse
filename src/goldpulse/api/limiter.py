import ipaddress

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request


def _internal(peer: str) -> bool:
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def client_ip(request: Request) -> str:
    """IP thật của người gọi. Sau Cloudflare Tunnel mọi request đến từ cloudflared trên cùng máy — peer là
    127.0.0.1 (make dev) hoặc IP bridge Docker 172.x / 192.168.65.x (production) → giới hạn theo peer sẽ khoá
    chung tất cả. Khi peer là nội bộ thì dùng CF-Connecting-IP. Cổng web chỉ bind 127.0.0.1 nên người ngoài
    không tới thẳng được để giả header này."""
    peer = get_remote_address(request)
    if _internal(peer):
        return request.headers.get("cf-connecting-ip", peer)
    return peer


limiter = Limiter(key_func=client_ip)
