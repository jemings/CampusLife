"""campuslife-hub tailscale-check (#4 decision 9) against fixed `tailscale serve status --json` and `tailscale status --json`
output; CI has no tailscale. The shapes follow tailscale's ServeConfig and Status as read from its source, so they are an
inference #39 confirms on a real tailnet."""

import json
import subprocess

import pytest

from campuslife_hub import admin_client, cli, tailscale_check
from campuslife_hub.admin_client import AdminRefused, HubDidNotAnswer
from campuslife_hub.admin_token import AdminTokenUnusable
from campuslife_hub.tailscale_check import TailscaleUnavailable, check, hub_url, judge

NAME = "hub-laptop.tail1234.ts.net"


@pytest.fixture(autouse=True)
def root_without_a_hub(tmp_path, monkeypatch):
    """The command asks the Hub serving the data root for its public_url; no test's default root holds a Hub."""
    monkeypatch.setenv("CAMPUSLIFE_ROOT", str(tmp_path / "CampusLife"))
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


def on_8443(proxy=None, forward=None):
    """GOOD, plus port 8443 serving / through proxy or forwarding raw TCP to forward."""
    if forward is not None:
        return {**GOOD, "TCP": {**GOOD["TCP"], "8443": {"TCPForward": forward}}}
    return {**GOOD, "TCP": {**GOOD["TCP"], "8443": {"HTTPS": True}}, "Web": {**GOOD["Web"], f"{NAME}:8443": {"Handlers": {"/": {"Proxy": proxy}}}}}


ADMIN_MAPPED = "admin port 8731"


@pytest.mark.parametrize(
    "serve",
    [
        serving(("/", "http://127.0.0.1:8731")),
        serving(("/", "http://127.0.0.1:8730"), ("/admin", "http://127.0.0.1:8731")),
        on_8443(proxy="http://127.0.0.1:8731"),
        on_8443(proxy="http://localhost:8731"),
        on_8443(proxy="http://[::1]:8731"),
        on_8443(forward="127.0.0.1:8731"),
        on_8443(forward="localhost:8731"),
        on_8443(forward="[::1]:8731"),
    ],
    ids=["443 to admin", "a path to admin", "another port to admin", "localhost", "ipv6 loopback", "a TCP forward to admin",
         "a TCP forward to localhost", "a TCP forward to ipv6 loopback"],
)
def test_any_mapping_of_the_admin_port_is_a_problem_naming_it(serve):
    verdict = judged(serve)

    assert not verdict.ok
    assert any(ADMIN_MAPPED in problem for problem in verdict.problems), verdict.problems


@pytest.mark.parametrize(
    "target", ["http://[::ffff:127.0.0.1]:8731", "http://127.1.2.3:8731", "[::ffff:127.0.0.1]:8731"],
    ids=["ipv4-mapped ipv6 loopback", "another 127/8 address", "a TCP forward to ipv4-mapped loopback"],
)
def test_every_spelling_of_a_loopback_address_is_named_the_admin_port(target):
    serve = on_8443(forward=target) if "://" not in target else on_8443(proxy=target)

    assert any(ADMIN_MAPPED in problem for problem in judged(serve).problems)


def with_service(service):
    """GOOD, plus a Tailscale Service (ServeConfig.Services, tailscale ipn/serve.go) configured as given."""
    return {**GOOD, "Services": {"svc:campuslife": service}}


@pytest.mark.parametrize(
    "service",
    [
        {"TCP": {"443": {"HTTPS": True}}, "Web": {"campuslife.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8731"}}}}},
        {"TCP": {"8731": {"TCPForward": "127.0.0.1:8731"}}},
    ],
    ids=["a site to admin", "a TCP forward to admin"],
)
def test_a_tailscale_service_reaching_the_admin_port_is_a_problem(service):
    verdict = judged(with_service(service))

    assert not verdict.ok
    assert any(ADMIN_MAPPED in problem for problem in verdict.problems)


def test_a_tailscale_service_offering_the_sync_listener_is_left_alone():
    service = {"TCP": {"443": {"HTTPS": True}}, "Web": {"campuslife.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8730"}}}}}

    verdict = judged(with_service(service))

    assert (verdict.problems, verdict.warnings) == ((), ())


def test_a_tailscale_service_does_not_stand_in_for_the_machines_own_443():
    service = {"TCP": {"443": {"HTTPS": True}}, "Web": {"campuslife.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8730"}}}}}

    assert not judged({"Services": {"svc:campuslife": service}}).ok


@pytest.mark.parametrize("target", ["http://192.168.0.10:8731", "http://127.0.0.1:18731"], ids=["another machine", "another port"])
def test_a_mapping_elsewhere_is_not_named_the_admin_port(target):
    assert not [problem for problem in judged(on_8443(proxy=target)).problems if ADMIN_MAPPED in problem]


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


@pytest.mark.parametrize(
    "listener", [{"TCPForward": "127.0.0.1:8730"}, {"HTTP": True}, {}], ids=["raw TCP", "plain http", "neither"],
)
def test_443_that_serve_does_not_terminate_as_https_is_a_problem(listener):
    verdict = judged(serving(("/", "http://127.0.0.1:8730"), tcp={"443": listener}))

    assert not verdict.ok
    assert any("port 443 is not https" in problem for problem in verdict.problems)


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


def test_a_public_url_matching_the_name_raises_no_warning():
    assert judged(GOOD, public_url=f"https://{NAME}").warnings == ()


def test_a_public_url_is_compared_as_serve_would_hand_it_out():
    """The command refuses a trailing slash (device/pairing_offer hub_url), so nothing is trimmed before comparing."""
    assert judged(GOOD, public_url=f"https://{NAME}/").warnings != ()


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


@pytest.mark.parametrize("printed", ["No serve config", "[]", "null"], ids=["not json", "a list", "null"])
def test_output_that_is_not_a_json_object_is_tailscale_unavailable(printed):
    with pytest.raises(TailscaleUnavailable):
        check(lambda arguments: printed, sync_port=8730, admin_port=8731, public_url=None)


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


def serving_hub_with(public_url):
    """admin_client.call as the Hub serving the data root answers GET /api/status, started with public_url."""

    def call(layout, method, target, body=None):
        assert (method, target) == ("GET", "/api/status")
        return {"devices": {"public_url": public_url, "dormant_after_days": 90}}

    return call


def warnings_in(printed):
    return [line for line in printed.splitlines() if line.startswith("warning:")]


def test_without_a_public_url_given_the_command_compares_the_one_the_serving_hub_hands_out(monkeypatch, capsys):
    run, _ = fake_tailscale()
    monkeypatch.setattr(tailscale_check, "tailscale", run)
    monkeypatch.setattr(admin_client, "call", serving_hub_with("https://old-name.tail1234.ts.net"))

    assert cli.main(["tailscale-check"]) == 0

    printed = capsys.readouterr().out
    assert "public_url https://old-name.tail1234.ts.net (the serving Hub's)" in printed
    assert len(warnings_in(printed)) == 1 and "old-name" in warnings_in(printed)[0]


def test_a_serving_hub_handing_out_this_machines_name_raises_no_warning(monkeypatch, capsys):
    run, _ = fake_tailscale()
    monkeypatch.setattr(tailscale_check, "tailscale", run)
    monkeypatch.setattr(admin_client, "call", serving_hub_with(f"https://{NAME}"))

    assert cli.main(["tailscale-check"]) == 0
    assert warnings_in(capsys.readouterr().out) == []


def test_a_serving_hub_started_without_a_public_url_is_a_warning_saying_what_to_give_it(monkeypatch, capsys):
    run, _ = fake_tailscale()
    monkeypatch.setattr(tailscale_check, "tailscale", run)
    monkeypatch.setattr(admin_client, "call", serving_hub_with(None))

    assert cli.main(["tailscale-check"]) == 0

    [warning] = warnings_in(capsys.readouterr().out)
    assert "no device can be added" in warning and f"--public-url https://{NAME}" in warning


def test_with_no_hub_serving_and_no_public_url_given_nothing_is_compared(monkeypatch, capsys, tmp_path):
    run, _ = fake_tailscale()
    monkeypatch.setattr(tailscale_check, "tailscale", run)

    assert cli.main(["tailscale-check"]) == 0

    printed = capsys.readouterr().out
    assert f"public_url - (no Hub serves {(tmp_path / 'CampusLife').resolve()}; give --public-url to compare one)" in printed
    assert warnings_in(printed) == []


def test_a_public_url_given_is_compared_without_asking_the_hub(monkeypatch, capsys):
    run, _ = fake_tailscale()
    monkeypatch.setattr(tailscale_check, "tailscale", run)

    def unasked(*arguments):
        raise AssertionError("the Hub was asked")

    monkeypatch.setattr(admin_client, "call", unasked)

    assert cli.main(["tailscale-check", "--public-url", f"https://{NAME}"]) == 0
    assert f"public_url https://{NAME} (given)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "failure",
    [AdminTokenUnusable("the admin token file cannot be read"), HubDidNotAnswer("the Hub did not answer in time"), AdminRefused("refused")],
)
def test_a_hub_that_cannot_be_asked_is_one_line_and_the_exposure_is_still_checked(monkeypatch, capsys, failure):
    run, _ = fake_tailscale(serve=serving(("/", "http://127.0.0.1:8731")))
    monkeypatch.setattr(tailscale_check, "tailscale", run)

    def failing(*arguments):
        raise failure

    monkeypatch.setattr(admin_client, "call", failing)

    assert cli.main(["tailscale-check"]) == 1

    printed = capsys.readouterr().out
    assert any(ADMIN_MAPPED in line for line in printed.splitlines() if line.startswith("problem:"))
    assert warnings_in(printed) == [f"warning: could not ask the running Hub: {failure}"]
    assert "public_url - (not compared)" in printed


def test_a_hub_that_cannot_be_asked_leaves_a_good_setup_ok(monkeypatch, capsys):
    run, _ = fake_tailscale()
    monkeypatch.setattr(tailscale_check, "tailscale", run)

    def failing(*arguments):
        raise AdminTokenUnusable("the admin token file cannot be read")

    monkeypatch.setattr(admin_client, "call", failing)

    assert cli.main(["tailscale-check"]) == 0
    assert "ok:" in capsys.readouterr().out
