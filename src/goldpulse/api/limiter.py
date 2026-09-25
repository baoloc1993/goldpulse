import ipaddress

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from ..config import settings


def _internal(peer: str) -> bool:
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def client_ip(request: Request) -> str:
    """IP để giới hạn số lần thử. Mặc định (chạy local, không Tunnel) dùng peer — một người vận hành chung một
    bucket là đúng. TRUST_CLOUDFLARE_IP=true (đứng sau Cloudflare Tunnel): peer là cloudflared trên cùng máy
    (127.0.0.1 / IP bridge Docker) → dùng CF-Connecting-IP; chỉ tin khi peer là nội bộ."""
    peer = get_remote_address(request)
    if settings.trust_cloudflare_ip and _internal(peer):
        return request.headers.get("cf-connecting-ip", peer)
    return peer


limiter = Limiter(key_func=client_ip)
