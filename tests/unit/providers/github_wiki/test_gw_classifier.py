"""Unit tests for the git failure classifier and the push porcelain parser."""

from __future__ import annotations

import inspect
import re

import pytest

from wikiops.providers.github_wiki import classifier
from wikiops.providers.github_wiki.classifier import (
    PushRefResult,
    classify,
    parse_push_porcelain,
    stderr_tail,
)
from wikiops.providers.github_wiki.errors import CODES, GithubWikiError
from wikiops.providers.github_wiki.ports import CommandResult
from wikiops.providers.github_wiki.redaction import MASK, Redactor

MESSAGE_SHAPE = re.compile(r"^\[github_wiki:[a-z_]+\.[a-z_]+\] .+ Hint: .+\.$")
PLAIN = Redactor(())

NOISE_BEFORE = "Cloning into 'wiki'...\nremote: Enumerating objects: 5, done.\n"
NOISE_AFTER = "\nPlease check your configuration.\n"

# -- recorded stderr samples: one HTTPS, one SSH and one file:// per class ---

NOT_FOUND = [
    pytest.param(
        "remote: Repository not found.\n"
        "fatal: repository 'https://github.com/acme/platform.wiki.git/' not found\n",
        id="https",
    ),
    pytest.param(
        "ERROR: Repository not found.\n"
        "fatal: Could not read from remote repository.\n\n"
        "Please make sure you have the correct access rights\nand the repository exists.\n",
        id="ssh",
    ),
    pytest.param(
        "fatal: '/tmp/remotes/acme/platform.wiki.git' does not appear to be a git repository\n"
        "fatal: Could not read from remote repository.\n\n"
        "Please make sure you have the correct access rights\nand the repository exists.\n",
        id="file",
    ),
    pytest.param("fatal: REPOSITORY NOT FOUND\n", id="upper-case-tolerated"),
]

AUTH_REJECTED = [
    pytest.param(
        "remote: Invalid username or password.\n"
        "fatal: Authentication failed for 'https://github.com/acme/platform.wiki.git/'\n",
        id="https-authentication-failed",
    ),
    pytest.param(
        "fatal: unable to access 'https://github.com/acme/platform.wiki.git/': "
        "The requested URL returned error: 403\n",
        id="https-403",
    ),
    pytest.param(
        "fatal: unable to access 'https://github.com/acme/platform.wiki.git/': "
        "The requested URL returned error: 401\n",
        id="https-401",
    ),
    pytest.param(
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n",
        id="https-no-credentials",
    ),
    pytest.param("remote: HTTP Basic: Access denied\n", id="https-basic-denied"),
    pytest.param(
        "git@github.com: Permission denied (publickey).\n"
        "fatal: Could not read from remote repository.\n\n"
        "Please make sure you have the correct access rights\nand the repository exists.\n",
        id="ssh-publickey",
    ),
    pytest.param(
        "git@ghe.acme.io: Permission denied (publickey,password).\n"
        "fatal: Could not read from remote repository.\n",
        id="ssh-publickey-and-password",
    ),
]

HOST_KEY = [
    pytest.param(
        "Host key verification failed.\nfatal: Could not read from remote repository.\n"
        "\nPlease make sure you have the correct access rights\nand the repository exists.\n",
        id="ssh",
    ),
]

NETWORK = [
    pytest.param(
        "fatal: unable to access 'https://github.com/acme/platform.wiki.git/': "
        "Could not resolve host: github.com\n",
        id="https-dns",
    ),
    pytest.param(
        "fatal: unable to access 'https://github.com/acme/platform.wiki.git/': "
        "Failed to connect to github.com port 443 after 21 ms: Connection refused\n",
        id="https-refused",
    ),
    pytest.param(
        "fatal: unable to access 'https://github.com/acme/platform.wiki.git/': "
        "Failed to connect to github.com port 443: Connection timed out\n",
        id="https-timeout",
    ),
    pytest.param(
        "ssh: Could not resolve hostname github.com: Name or service not known\n"
        "fatal: Could not read from remote repository.\n",
        id="ssh-dns",
    ),
    pytest.param(
        "ssh: connect to host github.com port 22: Connection timed out\n"
        "fatal: Could not read from remote repository.\n",
        id="ssh-timeout",
    ),
    pytest.param(
        "ssh: connect to host github.com port 22: Network is unreachable\n"
        "fatal: Could not read from remote repository.\n",
        id="ssh-unreachable",
    ),
]

UNCLASSIFIED = "fatal: something exotic and unforeseen happened\n"


def failed(stderr: str = "", stdout: str = "", returncode: int = 128) -> CommandResult:
    return CommandResult(("git", "fetch"), returncode, stdout, stderr)


def error_for(operation: str, result: CommandResult, **kwargs: object) -> GithubWikiError:
    return classify(operation, result, redactor=PLAIN, **kwargs)  # type: ignore[arg-type]


# -- one table: each class maps to its code, noise or not, on every transport -

CLASS_CASES = [
    pytest.param(code, sample.values[0], id=f"{code}-{sample.id}")
    for code, group in (
        ("wiki.not_initialized", NOT_FOUND),
        ("auth.rejected", AUTH_REJECTED),
        ("auth.ssh_host_key", HOST_KEY),
        ("network.unreachable", NETWORK),
    )
    for sample in group
]


@pytest.mark.parametrize("operation", ["ls-remote", "clone", "fetch"])
@pytest.mark.parametrize(("code", "stderr"), CLASS_CASES)
def test_each_stderr_class_maps_to_its_code(operation: str, code: str, stderr: str) -> None:
    assert error_for(operation, failed(stderr)).code == code


@pytest.mark.parametrize(("code", "stderr"), CLASS_CASES)
def test_classification_tolerates_surrounding_noise(code: str, stderr: str) -> None:
    noisy = NOISE_BEFORE + stderr + NOISE_AFTER

    assert error_for("clone", failed(noisy)).code == code


def test_the_not_found_samples_cover_https_ssh_and_file_remotes() -> None:
    assert {"https", "ssh", "file"} <= {sample.id for sample in NOT_FOUND}


@pytest.mark.parametrize("stderr", NOT_FOUND)
def test_not_found_is_never_reported_as_an_auth_failure(stderr: str) -> None:
    assert error_for("clone", failed(stderr)).code == "wiki.not_initialized"


@pytest.mark.parametrize("stderr", AUTH_REJECTED)
def test_auth_failures_are_never_reported_as_a_missing_wiki(stderr: str) -> None:
    assert error_for("clone", failed(stderr)).code == "auth.rejected"


@pytest.mark.parametrize("stderr", NETWORK)
def test_connectivity_failures_are_network_unreachable_on_both_transports(stderr: str) -> None:
    assert error_for("fetch", failed(stderr)).code == "network.unreachable"


def test_an_unrecognised_failure_falls_back_to_git_failed() -> None:
    error = error_for("fetch", failed(UNCLASSIFIED))

    assert error.code == "sync.git_failed"
    assert "something exotic" in str(error)


@pytest.mark.parametrize("operation", ["ls-remote", "clone", "fetch", "merge", "status"])
def test_the_fallback_names_the_operation_for_every_non_push_non_commit_command(
    operation: str,
) -> None:
    error = error_for(operation, failed(UNCLASSIFIED))

    assert error.code == "sync.git_failed"
    assert f"op='{operation}'" in str(error)


def test_empty_stderr_still_yields_a_well_formed_error() -> None:
    error = error_for("fetch", failed(""))

    assert error.code == "sync.git_failed"
    assert MESSAGE_SHAPE.fullmatch(str(error))
    assert "stderr=" not in str(error)


# -- hints and message content ------------------------------------------------


def test_not_initialized_lists_the_four_causes_and_the_auth_label() -> None:
    error = error_for(
        "clone", failed(NOT_FOUND[0].values[0]), context={"auth": "gh:sgg10"}
    )

    text = str(error)
    assert error.code == "wiki.not_initialized"
    assert "not initialized" in text and "first page" in text
    assert "feature is disabled" in text
    assert "has no access" in text
    assert "owner, repo or host" in text
    assert "auth='gh:sgg10'" in text


def test_not_initialized_over_ssh_gives_the_same_message_as_over_https() -> None:
    https = error_for("clone", failed(NOT_FOUND[0].values[0]), context={"auth": "ambient"})
    ssh = error_for("clone", failed(NOT_FOUND[1].values[0]), context={"auth": "ambient"})

    assert https.summary == ssh.summary
    assert https.hint == ssh.hint


def test_https_auth_failure_hint_suggests_an_explicit_mode() -> None:
    error = error_for("fetch", failed(AUTH_REJECTED[0].values[0]))

    assert "env, gh or ssh" in error.hint


def test_ssh_auth_failure_hint_points_at_the_key_and_key_path() -> None:
    error = error_for("fetch", failed(AUTH_REJECTED[5].values[0]))

    assert "key_path" in error.hint
    assert error.code == "auth.rejected"


def test_host_key_failure_uses_the_default_hint_that_never_auto_accepts() -> None:
    error = error_for("fetch", failed(HOST_KEY[0].values[0]))

    assert error.hint == CODES["auth.ssh_host_key"].default_hint


def test_caller_context_is_rendered_in_the_message() -> None:
    error = error_for(
        "fetch",
        failed(NETWORK[0].values[0]),
        context={"remote": "https://github.com/acme/platform.wiki.git", "workdir": "/w"},
    )

    assert "remote='https://github.com/acme/platform.wiki.git'" in str(error)
    assert "workdir='/w'" in str(error)


@pytest.mark.parametrize("operation", ["clone", "fetch", "push", "commit"])
def test_every_classified_error_matches_the_gw_p12_shape(operation: str) -> None:
    for stderr in (
        NOT_FOUND[0].values[0],
        AUTH_REJECTED[1].values[0],
        NETWORK[3].values[0],
        UNCLASSIFIED,
        "",
    ):
        error = error_for(operation, failed(stderr))

        assert MESSAGE_SHAPE.fullmatch(str(error)), str(error)
        assert "\n" not in str(error)


# -- stderr tail and redaction --------------------------------------------------


def test_the_stderr_tail_keeps_only_the_last_ten_lines() -> None:
    stderr = "\n".join(f"line {number}" for number in range(1, 26))

    tail = stderr_tail(stderr, PLAIN)

    assert tail.splitlines() == [f"line {number}" for number in range(16, 26)]


def test_the_stderr_tail_drops_blank_lines_and_trailing_whitespace() -> None:
    assert stderr_tail("a\n\n\nb  \n\n", PLAIN) == "a\nb"


def test_the_stderr_tail_limit_is_adjustable() -> None:
    assert stderr_tail("a\nb\nc\nd", PLAIN, limit=2) == "c\nd"


def test_the_stderr_tail_is_redacted() -> None:
    redactor = Redactor(("sekrit-token",))

    assert stderr_tail("fatal: bad sekrit-token here", redactor) == f"fatal: bad {MASK} here"


def test_the_error_message_carries_only_the_tail_and_never_a_secret() -> None:
    token = "ghp_TopSecret999"
    lines = [f"noise {number}" for number in range(30)] + [f"fatal: echoed {token}"]
    result = failed("\n".join(lines))

    error = classify("fetch", result, redactor=Redactor((token,)))

    assert token not in str(error)
    assert MASK in str(error)
    assert "noise 29" in str(error)
    assert "noise 5" not in str(error)
    assert MESSAGE_SHAPE.fullmatch(str(error))


def test_secrets_in_caller_context_are_redacted_too() -> None:
    token = "ghp_TopSecret999"

    error = classify(
        "fetch",
        failed(UNCLASSIFIED),
        redactor=Redactor((token,)),
        context={"remote": f"https://x:{token}@github.com/a/b"},
    )

    assert token not in str(error)


# -- timeouts and invalid use ----------------------------------------------------


@pytest.mark.parametrize("operation", ["ls-remote", "clone", "fetch", "merge", "commit"])
def test_a_timeout_on_a_non_push_command_is_sync_timeout_naming_the_operation(
    operation: str,
) -> None:
    result = CommandResult(("git", operation), -9, "", "partial output", timed_out=True)

    error = error_for(operation, result)

    assert error.code == "sync.timeout"
    assert f"git {operation} timed out" in str(error)


def test_a_timeout_wins_over_whatever_the_partial_stderr_looks_like() -> None:
    result = CommandResult(("git", "fetch"), -9, "", NOT_FOUND[0].values[0], timed_out=True)

    assert error_for("fetch", result).code == "sync.timeout"


def test_a_timeout_on_push_is_push_failed_naming_the_timeout() -> None:
    result = CommandResult(("git", "push"), -9, "", "", timed_out=True)

    error = error_for("push", result, context={"sha": "abc1234", "workdir": "/w"})

    assert error.code == "push.failed"
    assert "timed out" in str(error)
    assert "sha='abc1234'" in str(error)


def test_classifying_a_successful_command_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="successful"):
        error_for("fetch", CommandResult(("git", "fetch"), 0, "", ""))


# -- commit -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stderr",
    [
        "Author identity unknown\n\n*** Please tell me who you are.\n\nRun\n"
        '  git config --global user.email "you@example.com"\n',
        "fatal: unable to auto-detect email address (got 'me@host.(none)')\n",
        "fatal: empty ident name (for <>) not allowed\n",
    ],
    ids=["tell-me-who-you-are", "auto-detect-email", "empty-ident-name"],
)
def test_a_missing_git_identity_is_commit_identity_missing(stderr: str) -> None:
    error = error_for("commit", failed(stderr, returncode=128))

    assert error.code == "commit.identity_missing"
    assert "bot" in error.hint


def test_any_other_commit_failure_is_commit_failed_with_the_redacted_tail() -> None:
    error = error_for(
        "commit",
        failed("error: pre-commit hook exited with status 1\n", returncode=1),
        context={"workdir": "/w"},
    )

    assert error.code == "commit.failed"
    assert "pre-commit hook" in str(error)
    assert "workdir='/w'" in str(error)


def test_commit_never_uses_the_network_classes() -> None:
    error = error_for("commit", failed(NETWORK[0].values[0]))

    assert error.code == "commit.failed"


# -- push -----------------------------------------------------------------------------

REJECTED_FETCH_FIRST = (
    "To https://github.com/acme/platform.wiki.git\n"
    "!\trefs/heads/master:refs/heads/master\t[rejected] (fetch first)\n"
    "Done\n"
)


def pushed(stdout: str = "", stderr: str = "", returncode: int = 1) -> CommandResult:
    return CommandResult(("git", "push", "--porcelain"), returncode, stdout, stderr)


def test_the_porcelain_parser_reads_a_rejected_ref() -> None:
    assert parse_push_porcelain(REJECTED_FETCH_FIRST) == (
        PushRefResult(
            flag="!",
            ref="refs/heads/master:refs/heads/master",
            summary="[rejected] (fetch first)",
        ),
    )


def test_the_porcelain_parser_reads_every_flag_and_ignores_noise() -> None:
    stdout = (
        "To file:///remote.git\n"
        "*\trefs/heads/new:refs/heads/new\t[new branch]\n"
        " \trefs/heads/a:refs/heads/a\tabc1234..def5678\n"
        "=\trefs/heads/b:refs/heads/b\t[up to date]\n"
        "!\trefs/heads/c:refs/heads/c\t[remote rejected] (pre-receive hook declined)\n"
        "this line is not porcelain\n"
        "Done\n"
    )

    assert [(r.flag, r.ref.split(":")[0]) for r in parse_push_porcelain(stdout)] == [
        ("*", "refs/heads/new"),
        (" ", "refs/heads/a"),
        ("=", "refs/heads/b"),
        ("!", "refs/heads/c"),
    ]


def test_the_porcelain_parser_tolerates_crlf_line_endings() -> None:
    results = parse_push_porcelain(REJECTED_FETCH_FIRST.replace("\n", "\r\n"))

    assert [r.summary for r in results] == ["[rejected] (fetch first)"]


def test_the_porcelain_parser_returns_nothing_for_text_without_ref_lines() -> None:
    assert parse_push_porcelain("fatal: unable to access\nDone\n") == ()


@pytest.mark.parametrize("reason", ["fetch first", "non-fast-forward"])
def test_a_rejected_ref_is_push_rejected(reason: str) -> None:
    stdout = f"To x\n!\trefs/heads/master:refs/heads/master\t[rejected] ({reason})\nDone\n"

    error = error_for("push", pushed(stdout), context={"sha": "abc1234", "workdir": "/w"})

    assert error.code == "push.rejected"
    assert "sha='abc1234'" in str(error) and "workdir='/w'" in str(error)
    assert f"({reason})" in str(error) or reason in str(error)
    assert MESSAGE_SHAPE.fullmatch(str(error))


def test_a_remote_rejection_is_push_failed_not_push_rejected() -> None:
    stdout = (
        "To x\n"
        "!\trefs/heads/master:refs/heads/master\t[remote rejected] (pre-receive hook declined)\n"
        "Done\n"
    )

    error = error_for("push", pushed(stdout))

    assert error.code == "push.failed"
    assert "pre-receive hook declined" in str(error)


def test_the_porcelain_line_wins_over_what_stderr_says() -> None:
    stderr = "git@github.com: Permission denied (publickey).\n"

    error = error_for("push", pushed(REJECTED_FETCH_FIRST, stderr))

    assert error.code == "push.rejected"


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        (AUTH_REJECTED[1].values[0], "auth.rejected"),
        (AUTH_REJECTED[5].values[0], "auth.rejected"),
        (NETWORK[1].values[0], "network.unreachable"),
        (NETWORK[4].values[0], "network.unreachable"),
        (NOT_FOUND[0].values[0], "wiki.not_initialized"),
        (HOST_KEY[0].values[0], "auth.ssh_host_key"),
        (UNCLASSIFIED, "push.failed"),
        ("", "push.failed"),
    ],
    ids=[
        "https-auth",
        "ssh-auth",
        "https-network",
        "ssh-network",
        "not-found",
        "host-key",
        "unclassified",
        "silent",
    ],
)
def test_without_a_porcelain_line_push_failures_use_the_stderr_classes(
    stderr: str, code: str
) -> None:
    assert error_for("push", pushed("", stderr)).code == code


def test_push_failures_carry_the_sha_and_workdir_context_for_every_class() -> None:
    context = {"sha": "abc1234", "workdir": "/w"}

    for stderr in (AUTH_REJECTED[1].values[0], NETWORK[1].values[0], UNCLASSIFIED):
        text = str(error_for("push", pushed("", stderr), context=context))

        assert "sha='abc1234'" in text and "workdir='/w'" in text


def test_push_output_is_redacted_like_everything_else() -> None:
    token = "ghp_TopSecret999"
    result = pushed(
        "", f"fatal: unable to access 'https://x-access-token:{token}@github.com/a/b.git/'"
    )

    error = classify("push", result, redactor=Redactor((token,)))

    assert token not in str(error)


# -- the ssh publickey marker has one source ---------------------------------


def test_the_ssh_publickey_marker_is_defined_once_and_shared_by_both_uses() -> None:
    source = inspect.getsource(classifier).lower()
    assert source.count("permission denied") == 1  # one regex, not a copy per use

    ssh_denied = CommandResult(
        ("git", "fetch"), 128, "", "git@github.com: Permission denied (publickey).\n", False
    )
    error = classify("fetch", ssh_denied, redactor=PLAIN)
    assert error.code == "auth.rejected"
    assert "auth.key_path" in error.hint  # the ssh-specific hint reads the same marker
