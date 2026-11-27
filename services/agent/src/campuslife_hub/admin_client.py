"""The CLI's way into a serving Hub (E11): its admin API on 127.0.0.1.

While a Hub holds the data root's hub.lock, the CLI does not touch the database; it asks that Hub,
at the port it left in admin.port. A lock nobody holds means no Hub serves: the port left behind
is stale and is removed before the lock is let go. A program that holds the port is trusted only
once it proves it holds the admin token for that port (see admin_auth), and the token is never
sent: each request is signed instead.
Proxies from the environment are not used: the request never leaves this machine.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import re
import secrets
import urllib.error
import urllib.request

from . import admin_token
from .admin_auth import HELLO_PATH, hello_mac, hub_proof, listener_address, signed_authorization
from .data_root import Layout
from .lock import AlreadyRunning, HubLock

REQUEST_TIMEOUT_S = 600.0
"""An import copies its files before the Hub answers."""


class HubNotServing(Exception):
    pass


class NotTheHub(Exception):
    pass


class AdminRefused(Exception):
    pass


class HubDidNotAnswer(Exception):
    pass


def _no_hub_holds_the_lock(layout: Layout) -> bool:
    """Whether no Hub holds the lock; the port a Hub left is then removed while the lock is held, so a Hub that takes
    the lock next keeps the port it leaves."""
    try:
        lock = HubLock.acquire(layout.lock_file)
    except AlreadyRunning:
        return False
    except FileNotFoundError:
        return True
    with lock, contextlib.suppress(FileNotFoundError):
        layout.admin_port.unlink()
    return True


def admin_address(layout: Layout) -> str:
    """The admin listener of the Hub that holds the lock; HubNotServing when none does, or it left no port."""
    if _no_hub_holds_the_lock(layout):
        raise HubNotServing(f"no Hub is serving {layout.root}; start it with `campuslife-hub serve`")
    try:
        port = int(layout.admin_port.read_text(encoding="ascii").strip())
    except (FileNotFoundError, ValueError):
        raise HubNotServing(f"no Hub is serving {layout.root}; start it with `campuslife-hub serve`") from None
    return listener_address(port)


def _exchange(url: str, method: str, target: str, body: bytes, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url + target, method=method, data=body or None, headers={**headers, "content-type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_S) as response:
            answer = json.load(response)
    except urllib.error.HTTPError as error:
        with error:
            try:
                reason = json.load(error).get("error")
            except (ValueError, AttributeError):
                reason = None
        raise AdminRefused(reason or f"the Hub answered {error.code}") from None
    except (TimeoutError, urllib.error.URLError) as error:
        if isinstance(error, TimeoutError) or isinstance(error.reason, TimeoutError):
            raise HubDidNotAnswer(
                f"the Hub at {url} did not answer {method} {target} within {REQUEST_TIMEOUT_S:.0f} s; it may still be doing it, "
                "so check with `campuslife-hub ls` before trying again"
            ) from None
        raise HubNotServing(f"the Hub at {url} does not answer ({error.reason}); start it with `campuslife-hub serve`") from None
    except ValueError:
        raise AdminRefused(f"the program at {url} answered {method} {target} with something other than JSON") from None
    if not isinstance(answer, dict):
        raise AdminRefused(f"the program at {url} answered {method} {target} with something other than a JSON object")
    return answer


def _hub_nonce(layout: Layout, address: str, token: str) -> str:
    """A nonce from the program at address, once it has proved it is the Hub listening there; NotTheHub when it cannot."""
    url, client_nonce = f"http://{address}", secrets.token_hex(32)
    not_the_hub = NotTheHub(f"the program answering on {url} is not the Hub of {layout.root}; nothing was sent to it")
    hello = {"nonce": client_nonce, "mac": hello_mac(token, address, client_nonce)}
    try:
        answer = _exchange(url, "POST", HELLO_PATH, json.dumps(hello).encode(), {})
    except AdminRefused:
        raise not_the_hub from None
    hub_nonce, proof = answer.get("nonce"), answer.get("proof")
    if not isinstance(hub_nonce, str) or not isinstance(proof, str):
        raise not_the_hub
    if not hmac.compare_digest(proof.encode(), hub_proof(token, address, client_nonce, hub_nonce).encode()):
        raise not_the_hub
    return hub_nonce


def _signed_call(layout: Layout, address: str, method: str, target: str, body: object) -> dict:
    token = admin_token.read(layout.admin_token)
    hub_nonce = _hub_nonce(layout, address, token)
    data = b"" if body is None else json.dumps(body).encode("utf-8")
    authorization = signed_authorization(token, address, hub_nonce, method, target, data)
    return _exchange(f"http://{address}", method, target, data, {"authorization": authorization})


def call(layout: Layout, method: str, target: str, body: object = None) -> dict:
    """The JSON the serving Hub answers; AdminRefused with its reason when it refuses."""
    return _signed_call(layout, admin_address(layout), method, target, body)


def login_url(layout: Layout) -> str:
    """A one-time login link to the serving Hub's local UI; NotTheHub when the answer links anywhere else."""
    address = admin_address(layout)
    url = f"http://{address}"
    login = _signed_call(layout, address, "POST", "/api/login", None).get("url")
    if not isinstance(login, str) or not re.fullmatch(re.escape(f"{url}/login?code=") + "[A-Za-z0-9_-]+", login):
        raise NotTheHub(f"the Hub at {url} answered a login link that is not its own; it was not opened")
    return login
