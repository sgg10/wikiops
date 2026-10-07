"""Unit tests for ``Redactor`` (GW-A7): nothing secret leaves the provider."""

from __future__ import annotations

import base64

import pytest

from wikiops.providers.github_wiki.redaction import MASK, Redactor

TOKEN = "ghp_SuperSecretToken123"
RAW_PAIR = f"x-access-token:{TOKEN}"
B64_PAIR = base64.b64encode(RAW_PAIR.encode()).decode()


def assert_clean(text: str) -> None:
    assert TOKEN not in text, text
    assert B64_PAIR not in text, text


# -- the token and the forms derived from it --------------------------------


def test_the_token_itself_is_masked() -> None:
    redacted = Redactor((TOKEN,)).redact(f"fatal: bad credential {TOKEN} rejected")

    assert redacted == f"fatal: bad credential {MASK} rejected"


def test_the_raw_x_access_token_pair_is_masked() -> None:
    redacted = Redactor((TOKEN,)).redact(f"using {RAW_PAIR} now")

    assert_clean(redacted)
    assert redacted == f"using {MASK} now"


def test_the_base64_x_access_token_pair_is_masked_even_when_only_the_token_is_known() -> None:
    redacted = Redactor((TOKEN,)).redact(f"header value {B64_PAIR} echoed")

    assert_clean(redacted)
    assert redacted == f"header value {MASK} echoed"


def test_every_listed_secret_is_masked_when_the_strategy_lists_all_forms() -> None:
    redactor = Redactor((TOKEN, B64_PAIR, RAW_PAIR))

    redacted = redactor.redact(f"a {TOKEN} b {B64_PAIR} c {RAW_PAIR} d")

    assert redacted == f"a {MASK} b {MASK} c {MASK} d"


def test_every_occurrence_is_masked_not_only_the_first() -> None:
    redacted = Redactor((TOKEN,)).redact(f"{TOKEN} and again {TOKEN}")

    assert redacted == f"{MASK} and again {MASK}"


def test_two_different_tokens_are_both_masked() -> None:
    other = "gho_AnotherToken456"

    redacted = Redactor((TOKEN, other)).redact(f"{TOKEN} / {other}")

    assert redacted == f"{MASK} / {MASK}"


def test_the_longer_form_wins_so_no_partial_token_text_is_left_behind() -> None:
    redacted = Redactor((TOKEN,)).redact(f"credential {RAW_PAIR}")

    assert "x-access-token" not in redacted


# -- the Authorization header ------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        f"AUTHORIZATION: basic {B64_PAIR}",
        f"Authorization: Basic {B64_PAIR}",
        f"authorization:basic {B64_PAIR}",
        "AUTHORIZATION: basic bm90LXRoZS1rbm93bi10b2tlbg==",
        "Authorization: Bearer ghs_unknownToTheRedactor",
    ],
    ids=["upper", "title", "no-space", "unknown-secret", "bearer"],
)
def test_the_whole_authorization_header_is_masked_without_knowing_the_secret(
    header: str,
) -> None:
    redacted = Redactor(()).redact(f"> {header}\n> Accept: */*")

    assert redacted == f"> {MASK}\n> Accept: */*"


def test_a_git_config_echo_of_the_extraheader_is_masked() -> None:
    echoed = f"http.https://github.com/.extraheader=AUTHORIZATION: basic {B64_PAIR}"

    redacted = Redactor((TOKEN,)).redact(echoed)

    assert_clean(redacted)
    assert redacted == f"http.https://github.com/.extraheader={MASK}"


# -- URL user-info -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://user:pw@github.com/o/r.wiki.git", f"https://{MASK}@github.com/o/r.wiki.git"),
        ("https://justatoken@github.com/o/r", f"https://{MASK}@github.com/o/r"),
        (
            "fatal: unable to access 'https://x-access-token:abc@ghe.io/o/r/': 403",
            f"fatal: unable to access 'https://{MASK}@ghe.io/o/r/': 403",
        ),
        ("ssh://git@github.com/o/r.git", f"ssh://{MASK}@github.com/o/r.git"),
        # An '@' inside the password: the user-info ends at the LAST '@' of the authority.
        ("https://user:p@ss@github.com/o/r", f"https://{MASK}@github.com/o/r"),
        ("https://user:p@@ss@github.com/o/r", f"https://{MASK}@github.com/o/r"),
        ("https://u:a@b@c:d@ghe.io:8443/o/r.git", f"https://{MASK}@ghe.io:8443/o/r.git"),
        (
            "fatal: unable to access 'https://x:pa@ss@github.com/o/r/': 403",
            f"fatal: unable to access 'https://{MASK}@github.com/o/r/': 403",
        ),
        ("remote: https://user:p@ss", f"remote: https://{MASK}@ss"),
    ],
    ids=[
        "user-password",
        "token-only",
        "inside-a-sentence",
        "ssh-scheme",
        "at-in-password",
        "double-at-in-password",
        "several-ats-and-port",
        "at-in-password-inside-a-sentence",
        "truncated-url",
    ],
)
def test_url_user_info_is_masked(text: str, expected: str) -> None:
    assert Redactor(()).redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "https://github.com/o/r.wiki.git",
        "git@github.com:o/r.wiki.git",
        "contact me@example.com about https://github.com/o/r",
        "https://github.com/o/r mailed to a@b.example",
        "https://github.com/o/r.wiki.git\nsee also a@b.example/path",
    ],
    ids=["https-no-userinfo", "scp-style-ssh", "email-outside-a-url", "email-after-url", "email-on-next-line"],
)
def test_text_without_url_user_info_is_left_alone(text: str) -> None:
    assert Redactor(()).redact(text) == text


# -- scope: stderr echo, wrapped exceptions, no-op ---------------------------


def test_a_git_stderr_echo_with_every_form_is_fully_clean() -> None:
    stderr = (
        f"Cloning into 'w'...\n"
        f"remote: invalid credentials for {TOKEN}\n"
        f"fatal: unable to access 'https://x-access-token:{TOKEN}@github.com/o/r.wiki.git/'\n"
        f"> AUTHORIZATION: basic {B64_PAIR}\n"
    )

    redacted = Redactor((TOKEN, B64_PAIR, RAW_PAIR)).redact(stderr)

    assert_clean(redacted)
    assert "Cloning into 'w'..." in redacted
    assert "github.com/o/r.wiki.git/" in redacted


def test_wrapped_exception_text_is_masked_like_any_other_text() -> None:
    try:
        raise RuntimeError(f"gh failed for {TOKEN}")
    except RuntimeError as exc:
        redacted = Redactor((TOKEN,)).redact(str(exc))

    assert redacted == f"gh failed for {MASK}"


def test_an_empty_secrets_tuple_leaves_plain_text_untouched() -> None:
    text = "fatal: repository not found\nhint: nothing secret here"

    assert Redactor(()).redact(text) == text
    assert Redactor().redact(text) == text


def test_empty_secret_strings_are_ignored_instead_of_masking_everything() -> None:
    assert Redactor(("",)).redact("plain text") == "plain text"


def test_the_redactor_is_immutable_and_reusable() -> None:
    redactor = Redactor((TOKEN,))

    first = redactor.redact(f"one {TOKEN}")
    second = redactor.redact(f"two {TOKEN}")

    assert (first, second) == (f"one {MASK}", f"two {MASK}")
