"""Validation fail-fast des URLs de service annoncées par les workers.

Une URL malformée (ex. coquille `http://1300.223.169.200:6000` dans la variable
`SERVICE_URL` d'un worker) ne doit jamais être réécrite en silence : elle est
rejetée à la source (démarrage du worker) et à la frontière (headnode).
"""
import ipaddress
import re
import urllib.parse

_DOTTED_NUMERIC = re.compile(r"^[0-9.]+$")


def validate_service_url(service_url: str) -> str:
    """Retourne `service_url` inchangée si valide, lève `ValueError` sinon."""
    if not isinstance(service_url, str) or not service_url.strip():
        raise ValueError(f"service_url must be a non-empty string, got {service_url!r}")
    parsed = urllib.parse.urlsplit(service_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"service_url must use http or https: {service_url!r}")
    if not parsed.hostname:
        raise ValueError(f"service_url has no host: {service_url!r}")
    # Lève ValueError si le port n'est pas un entier dans [0, 65535].
    parsed.port
    if _DOTTED_NUMERIC.match(parsed.hostname):
        # Lève ValueError pour toute IPv4 invalide (ex. octet 1300).
        ipaddress.IPv4Address(parsed.hostname)
    return service_url
