"""Serve a Hub for the Android sync e2e (#3 decision 4), run one command against it, then stop the Hub.

    uv run --project services/agent python apps/android/e2e/hub_session.py -- COMMAND...

The Hub gets a fresh data root and a pairing offer before it serves. COMMAND runs with CAMPUSLIFE_E2E_URL, the Hub's
sync listener on 127.0.0.1, and CAMPUSLIFE_E2E_OFFER, a file holding that offer (device/pairing_offer); the offer
pairs one Device within devices.OFFER_LIFETIME. The exit code is COMMAND's.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from campuslife_hub import devices
from campuslife_hub.core import open_core
from campuslife_hub.redaction import Redactor

PUBLIC_URL = "https://e2e-hub.tail0a1b2c.ts.net"
"""The hub_url the offer names. No tailscale serve runs here, so COMMAND reaches the Hub at CAMPUSLIFE_E2E_URL."""
READY_SECONDS = 60
STOP_SECONDS = 30
HUB_ID_HEADER = "CampusLife-Hub-Id"


async def _offer(root: Path) -> dict:
    async with open_core(root, create=True, sync_roots=[]) as core:
        offer = await devices.offer(core, now=datetime.now(UTC), public_url=PUBLIC_URL, redactor=Redactor())
    return offer.wire()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _hub(root: Path, *arguments: str) -> list[str]:
    return [sys.executable, "-m", "campuslife_hub.cli", "--root", str(root), *arguments]


def _answers(url: str) -> bool:
    """Whether the Hub answers at url: any answer it stamps with its id, here a 400 for the missing contract header."""
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with direct.open(f"{url}/v1/hub", timeout=5) as answer:
            return answer.headers.get(HUB_ID_HEADER) is not None
    except urllib.error.HTTPError as refused:
        return refused.headers.get(HUB_ID_HEADER) is not None
    except OSError:
        return False


def _serve(root: Path, port: int, log: Path) -> subprocess.Popen:
    with log.open("wb") as output:
        served = subprocess.Popen(
            _hub(root, "serve", "--public-url", PUBLIC_URL, "--sync-port", str(port), "--admin-port", "0"),
            stdout=output, stderr=subprocess.STDOUT,
        )
    deadline = time.monotonic() + READY_SECONDS
    while not _answers(f"http://127.0.0.1:{port}"):
        if served.poll() is not None or time.monotonic() > deadline:
            _stop(root, served)
            raise SystemExit(f"the Hub did not serve on port {port}:\n{log.read_text(errors='replace')}")
        time.sleep(0.2)
    return served


def _stop(root: Path, served: subprocess.Popen) -> None:
    if served.poll() is None:
        subprocess.run(_hub(root, "stop"), timeout=STOP_SECONDS, check=False)
    try:
        served.wait(timeout=STOP_SECONDS)
    except subprocess.TimeoutExpired:
        served.kill()
        served.wait()


def main(argv: list[str]) -> int:
    if "--" not in argv or argv.index("--") == len(argv) - 1:
        print(__doc__, file=sys.stderr)
        return 2
    command = argv[argv.index("--") + 1:]
    with tempfile.TemporaryDirectory(prefix="campuslife-e2e-") as scratch:
        root, offer, log = Path(scratch) / "CampusLife", Path(scratch) / "offer.json", Path(scratch) / "serve.out"
        offer.write_text(json.dumps(asyncio.run(_offer(root))), encoding="utf-8")
        port = _free_port()
        served = _serve(root, port, log)
        try:
            env = os.environ | {"CAMPUSLIFE_E2E_URL": f"http://127.0.0.1:{port}", "CAMPUSLIFE_E2E_OFFER": str(offer)}
            code = subprocess.run(command, env=env, check=False).returncode
        finally:
            _stop(root, served)
        if code != 0:
            print(f"--- the Hub's output ---\n{log.read_text(errors='replace')}", file=sys.stderr)
        return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
