"""campuslife-hub tailscale-check (#4 decision 9) against fixed `tailscale serve status --json` and `tailscale status --json`
output; CI has no tailscale. The shapes follow tailscale's ServeConfig and Status as read from its source, so they are an
inference #39 confirms on a real tailnet."""

import json
import subprocess

import pytest

from campuslife_hub import cli, tailscale_check
from campuslife_hub.tailscale_check import TailscaleUnavailable, check, hub_url, judge

NAME = "hub-laptop.tail1234.ts.net"
STATUS = {"BackendState": "Running", "Self": {"DNSName": f"{NAME}.", "HostName": "hub-laptop"}}


def serving(*handlers, tcp=None, funnel=None, foreground=None):
    """A ServeConfig whose 443 site has the handlers given as (path, proxy) pairs."""
    config = {
        "TCP": tcp if tcp is not None else {"443": {"HTTPS": True}},
        "Web": {f"{NAME}:443": {"Handlers": {path: {"Proxy": proxy} for path, proxy in handlers}}} if handlers else {},
    }
    if funnel is not None:
        config["AllowFunnel"] = funnel
    if foreground is not None:
        config["Foreground"] = foreground
    return config


GOOD = serving(("/", "http://127.0.0.1:8730"))


def judged(serve, status=STATUS, **options):
    return judge(serve, status, **{"sync_port": 8730, "admin_port": 8731, "public_url": None, **options})


def test_443_sent_only_to_the_sync_listener_passes_and_gives_the_expected_hub_url():
    verdict = judged(GOOD)

    assert (verdict.problems, verdict.warnings, verdict.hub_url) == ((), (), f"https://{NAME}")
    assert verdict.ok


def test_the_hub_url_is_the_magicdns_name_without_its_final_dot():
    assert hub_url(STATUS) == f"https://{NAME}"
    assert hub_url({"Self": {"DNSName": ""}}) is None
    assert hub_url({}) is None


@pytest.mark.parametrize(
    ("serve", "said"),
    [
        (serving(("/", "http://127.0.0.1:8731")), "admin"),
        (serving(("/", "http://127.0.0.1:8730"), ("/admin", "http://localhost:8731")), "admin"),
        (serving(("/", "http://127.0.0.1:8730"), tcp={"443": {"HTTPS": True}, "8443": {"TCPForward": "127.0.0.1:8731"}}), "admin"),
        ({**GOOD, "Web": {**GOOD["Web"], f"{NAME}:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8731"}}}},
          "TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}}}, "admin"),
    ],
    ids=["443 to admin", "a path to admin", "a TCP forward to admin", "another port to admin"],
)
def test_any_mapping_of_the_admin_port_is_a_problem(serve, said):
    verdict = judged(serve)

    assert not verdict.ok
    assert any(said in problem for problem in verdict.problems)


@pytest.mark.parametrize("funnel", [{f"{NAME}:443": True}, {f"{NAME}:8443": True}])
def test_funnel_is_a_problem(funnel):
    verdict = judged({**GOOD, "AllowFunnel": funnel})

    assert not verdict.ok
    assert any("Funnel" in problem for problem in verdict.problems)


def test_funnel_turned_off_is_no_problem():
    assert judged({**GOOD, "AllowFunnel": {f"{NAME}:443": False}}).ok


@pytest.mark.parametrize(
    "serve",
    [
        serving(("/", "http://127.0.0.1:9000")),
        serving(("/", "http://127.0.0.1:8730"), ("/files", "http://127.0.0.1:9000")),
        serving(("/v1", "http://127.0.0.1:8730")),
        serving(("/", "http://[::1]:8730")),
        serving(("/", "https+insecure://127.0.0.1:8730")),
        {**GOOD, "Web": {f"{NAME}:443": {"Handlers": {"/": {"Path": "C:\\Users\\me"}}}}},
    ],
    ids=["another port", "a second path elsewhere", "a path other than /", "not 127.0.0.1", "not http", "a folder"],
)
def test_443_going_anywhere_but_the_sync_listener_is_a_problem(serve):
    verdict = judged(serve)

    assert not verdict.ok
    assert any("8730" in problem for problem in verdict.problems)


@pytest.mark.parametrize(
    "serve",
    [{}, {"TCP": {}, "Web": {}}, serving(tcp={"443": {"HTTPS": True}}), serving(("/", "http://127.0.0.1:8730"), tcp={})],
    ids=["nothing served", "empty", "443 with no handler", "a handler with no 443 listener"],
)
def test_nothing_served_on_443_is_a_problem_naming_the_command_that_sets_it_up(serve):
    verdict = judged(serve)

    assert not verdict.ok
    assert any("tailscale serve --bg http://127.0.0.1:8730" in problem for problem in verdict.problems)


def test_443_forwarded_as_raw_tcp_is_a_problem():
    verdict = judged(serving(("/", "http://127.0.0.1:8730"), tcp={"443": {"TCPForward": "127.0.0.1:8730"}}))

    assert not verdict.ok


def test_a_serve_session_in_the_foreground_is_judged_too():
    verdict = judged({**GOOD, "Foreground": {"session-1": serving(("/", "http://127.0.0.1:8731"), tcp={"8443": {"HTTPS": True}})}})

    assert not verdict.ok


def test_another_sync_port_is_judged_against_that_port():
    assert judged(serving(("/", "http://127.0.0.1:9730")), sync_port=9730).ok
    assert not judged(GOOD, sync_port=9730).ok


@pytest.mark.parametrize(
    "status",
    [{"BackendState": "Running", "Self": {"DNSName": ""}}, {"BackendState": "NeedsLogin", "Self": {"DNSName": f"{NAME}."}}],
    ids=["no MagicDNS name", "logged out"],
)
def test_a_tailnet_without_a_name_or_not_running_is_a_problem(status):
    assert not judged(GOOD, status).ok


@pytest.mark.parametrize("public_url", [f"https://{NAME}", f"https://{NAME}/"])
def test_a_public_url_matching_the_name_raises_no_warning(public_url):
    assert judged(GOOD, public_url=public_url).warnings == ()


def test_a_public_url_other_than_the_name_is_a_warning_not_a_problem():
    verdict = judged(GOOD, public_url="https://old-name.tail1234.ts.net")

    assert verdict.ok
    assert [warning for warning in verdict.warnings if "old-name" in warning and NAME in warning]


def fake_tailscale(serve=GOOD, status=STATUS):
    asked = []

    def run(arguments):
        asked.append(tuple(arguments))
        return json.dumps({("serve", "status", "--json"): serve, ("status", "--json"): status}[tuple(arguments)])

    return run, asked


def test_check_reads_the_serve_config_and_the_status_through_the_command_it_is_given():
    run, asked = fake_tailscale()

    verdict = check(run, sync_port=8730, admin_port=8731, public_url=None)

    assert verdict.ok
    assert sorted(asked) == [("serve", "status", "--json"), ("status", "--json")]


def test_output_that_is_not_json_is_tailscale_unavailable():
    with pytest.raises(TailscaleUnavailable):
        check(lambda arguments: "No serve config", sync_port=8730, admin_port=8731, public_url=None)


@pytest.mark.parametrize(
    "outcome",
    [FileNotFoundError(2, "No such file"), subprocess.TimeoutExpired(["tailscale"], 15), subprocess.CompletedProcess(["tailscale"], 1, "", "not running")],
    ids=["not installed", "hangs", "fails"],
)
def test_a_tailscale_command_that_cannot_answer_is_tailscale_unavailable(monkeypatch, outcome):
    def run(*args, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(tailscale_check.subprocess, "run", run)

    with pytest.raises(TailscaleUnavailable):
        tailscale_check.tailscale(["status", "--json"])


def test_the_command_prints_the_expected_hub_url_and_succeeds(monkeypatch, capsys):
    run, _ = fake_tailscale()
    monkeypatch.setattr(tailscale_check, "tailscale", run)

    assert cli.main(["tailscale-check"]) == 0
    printed = capsys.readouterr().out
    assert f"hub_url https://{NAME}" in printed


def test_the_command_lists_each_problem_and_fails(monkeypatch, capsys):
    run, _ = fake_tailscale(serve={**serving(("/", "http://127.0.0.1:8731")), "AllowFunnel": {f"{NAME}:443": True}})
    monkeypatch.setattr(tailscale_check, "tailscale", run)

    assert cli.main(["tailscale-check"]) == 1
    captured = capsys.readouterr()
    assert [line for line in captured.out.splitlines() if line.startswith("problem:")][1:]
    assert "campuslife-hub:" in captured.err


def test_the_command_judges_the_ports_and_public_url_it_is_given(monkeypatch, capsys):
    run, _ = fake_tailscale(serve=serving(("/", "http://127.0.0.1:9730")))
    monkeypatch.setattr(tailscale_check, "tailscale", run)

    assert cli.main(["tailscale-check", "--sync-port", "9730", "--admin-port", "9731", "--public-url", "https://old.example.ts.net"]) == 0
    assert "warning:" in capsys.readouterr().out


def test_the_command_without_tailscale_says_so_and_fails(monkeypatch, capsys):
    def missing(arguments):
        raise TailscaleUnavailable("no tailscale command on PATH")

    monkeypatch.setattr(tailscale_check, "tailscale", missing)

    assert cli.main(["tailscale-check"]) == 1
    assert "no tailscale command" in capsys.readouterr().err
