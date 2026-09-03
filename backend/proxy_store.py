"""
Global proxy store — named proxies shared across all profiles.
Persisted as JSON in the app data dir; profiles reference proxies by name
or use a custom URL directly.
"""
import json
import os
import re

from config import APP_DATA_DIR

PROXIES_FILE = os.path.join(APP_DATA_DIR, 'proxies.json')

# socks5://host:port, socks5h://host:port, http://host:port, https://host:port
# with optional user:pass@ credentials.
_PROXY_RE = re.compile(
    r'^(socks5|socks5h|http|https)://([^:@/]+(:[^:@/]+)?@)?[a-zA-Z0-9._-]+:\d{1,5}$'
)
_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


def validate_url(url: str) -> str:
    url = url.strip()
    if not _PROXY_RE.match(url):
        raise ValueError(
            "Invalid proxy. Use e.g. socks5://127.0.0.1:1080 or http://user:pass@host:8080"
        )
    return url


def _load() -> dict:
    if os.path.exists(PROXIES_FILE):
        try:
            with open(PROXIES_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save(data: dict) -> None:
    tmp = PROXIES_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, PROXIES_FILE)


def list_proxies() -> list:
    return [{'name': k, 'url': v} for k, v in _load().items()]


def add_proxy(name: str, url: str) -> dict:
    name = name.strip()
    if not _NAME_RE.match(name):
        raise ValueError(
            "Proxy name: letters, numbers, dash and underscore only (max 64)."
        )
    url = validate_url(url)
    data = _load()
    data[name] = url
    _save(data)
    return {'name': name, 'url': url}


def delete_proxy(name: str) -> dict:
    data = _load()
    if name not in data:
        raise ValueError(f"Proxy '{name}' not found.")
    del data[name]
    _save(data)
    return {'status': 'deleted', 'name': name}


def resolve(value: str) -> str:
    """Resolve a profile's proxy value to a URL: a saved proxy name is looked
    up in the store; anything else is treated as a custom URL."""
    value = (value or '').strip()
    if not value:
        return ''
    store = _load()
    if value in store:
        return store[value]
    return value