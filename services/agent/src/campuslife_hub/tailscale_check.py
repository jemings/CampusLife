"""campuslife-hub tailscale-check (#4 decision 9): does `tailscale serve` expose the sync listener, and nothing else?

Read-only. It reads `tailscale serve status --json` (tailscale's ServeConfig) and `tailscale status --json` and judges:

- https on 443 is terminated by serve and goes, path / only, to http://127.0.0.1:<sync port>;
- no site, path or TCP forward on any port reaches the admin port, which is never exposed (D5);
- Funnel is off, so only the tailnet reaches the Hub;
- the tailnet is running and this machine has a MagicDNS name, whose https URL is the hub_url pairing hands out.

The hub_url pairing uses comes from `serve --public-url` (#19 decision 4); one given here that differs from the
MagicDNS name is a warning. A serve session run in the foreground is judged like the background config. The JSON
shapes are read from tailscale's source, not from a tailnet; #39 confirms them on real devices.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

type Run = Callable[[Sequence[str]], str]

TIMEOUT_S = 15
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})


class TailscaleUnavailable(Exception):
    """The tailscale command is missing, failed, or answered something that is not JSON."""


@dataclass(frozen=True)
class Verdict:
    problems: tuple[str, ...]
    warnings: tuple[str, ...]
    hub_url: str | None

    @property
    def ok(self) -> bool:
        return not self.problems


def tailscale(arguments: Sequence[str]) -> str:
    """What the tailscale command prints for the arguments."""
    try:
        finished = subprocess.run(["tailscale", *arguments], capture_output=True, text=True, errors="replace", timeout=TIMEOUT_S)
    except FileNotFoundError:
        raise TailscaleUnavailable("no tailscale command on PATH; install Tailscale and sign in to the tailnet") from None
    except subprocess.TimeoutExpired:
        raise TailscaleUnavailable(f"tailscale {' '.join(arguments)} did not answer within {TIMEOUT_S} seconds") from None
    if finished.returncode != 0:
        said = (finished.stderr or finished.stdout).strip() or f"exit status {finished.returncode}"
        raise TailscaleUnavailable(f"tailscale {' '.join(arguments)} failed: {said}")
    return finished.stdout


def hub_url(status: Mapping) -> str | None:
    """https://<MagicDNS name> from `tailscale status --json`; None without a name."""
    name = str((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
    return f"https://{name}" if name else None


def _configs(serve: Mapping) -> Iterator[Mapping]:
    yield serve
    for session in (serve.get("Foreground") or {}).values():
        yield session


def _reaches(target: str, port: int) -> bool:
    """Whether a proxy or TCP forward target, a URL or host:port, names a port on this machine."""
    parts = urlsplit(target if "://" in target else f"tcp://{target}")
    try:
        return parts.hostname in LOOPBACK_NAMES and parts.port == port
    except ValueError:
        return False


def judge(serve: Mapping, status: Mapping, *, sync_port: int, admin_port: int, public_url: str | None) -> Verdict:
    sync = f"http://127.0.0.1:{sync_port}"
    setup = f"run `tailscale serve --bg {sync}`"
    problems: list[str] = []
    served_443 = terminated_443 = False
    for config in _configs(serve):
        for port, listener in (config.get("TCP") or {}).items():
            forward = (listener or {}).get("TCPForward")
            if forward and _reaches(forward, admin_port):
                problems.append(f"port {port} forwards to the admin port {admin_port}, which must never be exposed")
            if str(port) == "443":
                if (listener or {}).get("HTTPS") and not forward:
                    terminated_443 = True
                else:
                    problems.append(f"port 443 is not https that serve terminates; {setup}")
        for site, web in (config.get("Web") or {}).items():
            port = site.rpartition(":")[2]
            for path, handler in ((web or {}).get("Handlers") or {}).items():
                proxy = (handler or {}).get("Proxy")
                where = f"https://{site}{path}"
                if proxy and _reaches(proxy, admin_port):
                    problems.append(f"{where} goes to the admin port {admin_port}, which must never be exposed")
                elif port == "443" and (path != "/" or proxy != sync):
                    problems.append(f"{where} goes to {proxy or 'something other than a proxy'}; on 443 only / goes, to {sync}")
                elif port == "443":
                    served_443 = True
        if any((config.get("AllowFunnel") or {}).values()):
            problems.append("Funnel is on, opening the Hub to the internet; turn it off so only the tailnet reaches it")
    if not (served_443 and terminated_443):
        problems.append(f"nothing serves https on 443 to the sync listener; {setup}")

    url = hub_url(status)
    if status.get("BackendState") not in (None, "Running"):
        problems.append(f"tailscale is {status.get('BackendState')}, not Running; sign in to the tailnet")
    if url is None:
        problems.append("this machine has no MagicDNS name; turn on MagicDNS and HTTPS certificates for the tailnet")
    warnings = []
    if public_url is not None and url is not None and public_url.rstrip("/") != url:
        warnings.append(f"--public-url is {public_url}, but the tailnet names this machine {url}; pairing would hand out the former")
    return Verdict(tuple(problems), tuple(warnings), url)


def _json(run: Run, arguments: Sequence[str]) -> Mapping:
    said = run(arguments)
    try:
        answer = json.loads(said)
    except ValueError:
        raise TailscaleUnavailable(f"tailscale {' '.join(arguments)} printed no JSON: {said[:200]!r}") from None
    if not isinstance(answer, Mapping):
        raise TailscaleUnavailable(f"tailscale {' '.join(arguments)} printed JSON that is not an object")
    return answer


def check(run: Run, *, sync_port: int, admin_port: int, public_url: str | None) -> Verdict:
    """Judge what the tailscale command run reports now."""
    return judge(
        _json(run, ["serve", "status", "--json"]), _json(run, ["status", "--json"]),
        sync_port=sync_port, admin_port=admin_port, public_url=public_url,
    )
