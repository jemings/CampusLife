"""The CLI's way into a serving Hub (E11): its admin API on 127.0.0.1.

While a Hub holds the data root's hub.lock, the CLI does not touch the database; it asks that Hub,
at the port it left in admin.port. A lock nobody holds means no Hub serves: the port left behind
is stale and is removed. A program that holds the port is trusted only once it proves it holds the
admin token (see admin_auth), and the token is never sent: each request is signed instead.
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
from .admin_auth import HELLO_PATH, hub_proof, signed_authorization
from .data_root import Layout
from .host import LOOPBACK
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


def _lock_is_free(layout: Layout) -> bool:
    try:
        lock = HubLock.acquire(layout.lock_file)
    except AlreadyRunning:
        return False
    except FileNotFoundError:
        return True
    lock.release()
    return True


def admin_url(layout: Layout) -> str:
    """The admin listener of the Hub that holds the lock; HubNotServing when none does, or it left no port."""
    if _lock_is_free(layout):
        with contextlib.suppress(FileNotFoundError):
            layout.admin_port.unlink()
        raise HubNotServing(f"no Hub is serving {layout.root}; start it with `campuslife-hub serve`")
    try:
        port = int(layout.admin_port.read_text(encoding="ascii").strip())
    except (FileNotFoundError, ValueError):
        raise HubNotServing(f"no Hub is serving {layout.root}; start it with `campuslife-hub serve`") from None
    return f"http://{LOOPBACK}:{port}"


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


def _hub_nonce(layout: Layout, url: str, token: str) -> str:
    """A nonce from the program at url, once it has proved it holds the token; NotTheHub when it cannot."""
    client_nonce = secrets.token_hex(32)
    not_the_hub = NotTheHub(f"the program answering on {url} is not the Hub of {layout.root}; nothing was sent to it")
    try:
        answer = _exchange(url, "POST", HELLO_PATH, json.dumps({"nonce": client_nonce}).encode(), {})
    except AdminRefused:
        raise not_the_hub from None
    hub_nonce, proof = answer.get("nonce"), answer.get("proof")
    if not isinstance(hub_nonce, str) or not isinstance(proof, str):
        raise not_the_hub
    if not hmac.compare_digest(proof.encode(), hub_proof(token, client_nonce, hub_nonce).encode()):
        raise not_the_hub
    return hub_nonce


def _signed_call(layout: Layout, url: str, method: str, target: str, body: object) -> dict:
    token = admin_token.read(layout.admin_token)
    hub_nonce = _hub_nonce(layout, url, token)
    data = b"" if body is None else json.dumps(body).encode("utf-8")
    path, _, query = target.partition("?")
    return _exchange(url, method, target, data, {"authorization": signed_authorization(token, hub_nonce, method, path, query, data)})


def call(layout: Layout, method: str, target: str, body: object = None) -> dict:
    """The JSON the serving Hub answers; AdminRefused with its reason when it refuses."""
    return _signed_call(layout, admin_url(layout), method, target, body)


def login_url(layout: Layout) -> str:
    """A one-time login link to the serving Hub's local UI; NotTheHub when the answer links anywhere else."""
    url = admin_url(layout)
    login = _signed_call(layout, url, "POST", "/api/login", None).get("url")
    if not isinstance(login, str) or not re.fullmatch(re.escape(f"{url}/login?code=") + "[A-Za-z0-9_-]+", login):
        raise NotTheHub(f"the Hub at {url} answered a login link that is not its own; it was not opened")
    return login
