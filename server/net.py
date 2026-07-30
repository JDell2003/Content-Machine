"""LAN address discovery.

The machine is LAN-only. DHCP can move the PC's address, so nothing hardcodes an
IP: the URL is re-derived on every start, printed to the console, written to
URL.txt, and shown on the status page. If the address ever shifts, one glance at
the PC gives the new bookmark.
"""
from __future__ import annotations

import socket
import subprocess
from pathlib import Path

from . import config

# RFC1918 ranges — a real LAN address, not loopback/link-local/CGNAT.
_PRIVATE_PREFIXES = ("192.168.", "10.")


def _is_private(ip: str) -> bool:
    if ip.startswith(_PRIVATE_PREFIXES):
        return True
    if ip.startswith("172."):
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


def _candidates() -> list[str]:
    found: list[str] = []
    # The UDP-connect trick: no packet is sent, but the OS picks the interface it
    # would route out of, which is the one the phone will reach us on.
    for probe in ("192.168.7.1", "8.8.8.8"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.4)
            s.connect((probe, 9))
            found.append(s.getsockname()[0])
        except OSError:
            pass
        finally:
            s.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except socket.gaierror:
        pass
    out: list[str] = []
    for ip in found:
        if ip and ip not in out and _is_private(ip) and not ip.startswith("100."):
            out.append(ip)
    return out


def lan_ip() -> str | None:
    """Best guess at the address the phone should use."""
    c = _candidates()
    # Prefer 192.168.* — that's what a home router hands out.
    for ip in c:
        if ip.startswith("192.168."):
            return ip
    return c[0] if c else None


def mdns_name() -> str:
    return f"{socket.gethostname().lower()}.local"


def urls() -> dict:
    ip = lan_ip()
    port = config.PORT
    return {
        "lan_ip": ip,
        "port": port,
        "primary": f"http://{ip}:{port}" if ip else None,
        "mdns": f"http://{mdns_name()}:{port}",
        "local": f"http://localhost:{port}",
        "hostname": socket.gethostname(),
    }


def write_url_file() -> Path:
    """URL.txt on disk so the bookmark is recoverable without reading logs."""
    u = urls()
    p = config.ROOT / "URL.txt"
    # ASCII only: this file gets opened in Notepad, and a stray em-dash shows up
    # as mojibake depending on which encoding the reader guesses.
    lines = [
        "Content Machine - bookmark this on your phone (same WiFi as the PC)",
        "",
        f"  {u['primary'] or 'NO LAN ADDRESS DETECTED — is WiFi connected?'}",
        "",
        f"PIN: {config.PIN}",
        "",
        f"also try (bonus, may not work): {u['mdns']}",
        f"on this PC:                    {u['local']}",
        "",
        f"hostname: {u['hostname']}",
        "If the address above ever changes, restart run.bat and re-read this file.",
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def banner() -> str:
    u = urls()
    bar = "=" * 52
    primary = u["primary"] or "!! no LAN address — check WiFi !!"
    return "\n".join([
        bar,
        "  Content Machine — LAN only",
        bar,
        f"  Phone (same WiFi):  {primary}",
        f"  This PC:            {u['local']}",
        f"  Bonus (mDNS):       {u['mdns']}",
        f"  PIN:                {config.PIN}",
        bar,
    ])


def try_enable_mdns() -> dict:
    """Report whether <hostname>.local resolves here. Windows' own mDNS support is
    inconsistent and it does not reliably *advertise* its name to iOS, so this is
    reported as a bonus, never relied on."""
    name = mdns_name()
    result = {"name": name, "resolves_locally": False, "bonjour_service": False}
    try:
        socket.setdefaulttimeout(2.0)
        socket.getaddrinfo(name, None, socket.AF_INET)
        result["resolves_locally"] = True
    except (socket.gaierror, OSError):
        pass
    try:
        out = subprocess.run(["sc", "query", "Bonjour Service"], capture_output=True,
                             text=True, timeout=15)
        result["bonjour_service"] = "RUNNING" in out.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    return result
