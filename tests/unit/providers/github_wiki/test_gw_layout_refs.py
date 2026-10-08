"""Unit tests for the page-reference policy of ``github_wiki`` (``layout.py``).

Covers the flat page namespace (GW-P9): check order, reserved/nested/markdown
rules and title-derived root references for ref-less creates.
"""

from __future__ import annotations

import re

import pytest
from wikiops_sdk.domain import DocumentRef, RefKind

from wikiops.core.exceptions import ConfigurationError
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.layout import derive_root_ref, validate_page_ref

MESSAGE_SHAPE = re.compile(r"^\[github_wiki:[a-z_]+\.[a-z_]+\] .+ Hint: .+\.$")


def path_ref(path: str | None, *, kind: RefKind = RefKind.PATH) -> DocumentRef:
    locator = {} if path is None else {"path": path}
    return DocumentRef(provider="wiki", kind=kind, locator=locator)


def failure(ref: DocumentRef) -> GithubWikiError:
    with pytest.raises(GithubWikiError) as caught:
        validate_page_ref(ref)
    return caught.value


# -- accepted references -----------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "Home.md",
        "guides-setup.md",
        "_Sidebar.md",
        "_Footer.md",
        "NOTES.MD",
        "Mixed.Md",
        "with space.md",
        "Ünï-cødé.md",
        "v1.2.notes.md",
        "git.md",
        ".gitignore.md",
    ],
)
def test_flat_markdown_page_is_accepted_and_returned_unchanged(path: str) -> None:
    assert validate_page_ref(path_ref(path)) == path


def test_sidebar_page_is_an_ordinary_page_in_this_change() -> None:
    assert validate_page_ref(path_ref("_Sidebar.md")) == "_Sidebar.md"


# -- ref.unsupported_kind / ref.missing_path ---------------------------------


@pytest.mark.parametrize("kind", [RefKind.ID, RefKind.ALIAS])
def test_non_path_kind_is_unsupported(kind: RefKind) -> None:
    error = failure(path_ref("Home.md", kind=kind))

    assert error.code == "ref.unsupported_kind"
    assert error.context["kind"] == kind.value
    assert "path reference" in error.hint


@pytest.mark.parametrize("path", [None, "", "   ", "\t\n"])
def test_path_ref_without_a_usable_path_is_missing_path(path: str | None) -> None:
    error = failure(path_ref(path))

    assert error.code == "ref.missing_path"
    assert "locator.path" in error.hint


# -- path.reserved ------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        "a/.git/x",
        ".git",
        ".GIT/config",
        "a/.Git",
        "a\\.git\\x",
        ".git/hooks/pre-push.md",
    ],
)
def test_git_component_is_reserved(path: str) -> None:
    error = failure(path_ref(path))

    assert error.code == "path.reserved"
    assert error.context["path"] == path


# -- path.nested_not_supported -----------------------------------------------


@pytest.mark.parametrize(
    ("path", "flat"),
    [
        ("guides/setup.md", "guides-setup.md"),
        ("a/b/c.md", "a-b-c.md"),
        ("guides\\setup.md", "guides-setup.md"),
        ("./x.md", "x.md"),
        ("/abs/x.md", "abs-x.md"),
        ("../x.md", "x.md"),
        ("a//b.md", "a-b.md"),
        ("a/b.txt", "a-b.md"),
        ("a/b", "a-b.md"),
    ],
)
def test_nested_path_is_rejected_with_the_flattened_name_in_the_hint(
    path: str, flat: str
) -> None:
    error = failure(path_ref(path))

    assert error.code == "path.nested_not_supported"
    assert error.context["path"] == path
    assert f"'{flat}'" in error.hint


def test_nested_path_is_never_flattened_automatically() -> None:
    with pytest.raises(GithubWikiError) as caught:
        validate_page_ref(path_ref("guides/setup.md"))

    assert caught.value.code == "path.nested_not_supported"


def test_nested_path_without_a_usable_flat_name_gets_the_generic_hint() -> None:
    error = failure(path_ref("/./"))

    assert error.code == "path.nested_not_supported"
    assert "separators" in error.hint


# -- path.not_markdown --------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "suggestion"),
    [
        ("notes.txt", "notes.md"),
        ("README", "README.md"),
        ("page.markdown", "page.md"),
        ("page.MDX", "page.md"),
        ("page.md.txt", "page.md.md"),
        ("md", "md.md"),
        ("image.png", "image.png.md"),
    ],
)
def test_page_without_md_suffix_is_not_markdown(path: str, suggestion: str) -> None:
    error = failure(path_ref(path))

    assert error.code == "path.not_markdown"
    assert error.context["path"] == path
    assert f"'{suggestion}'" in error.hint


# A stem that is empty or whitespace-only leaves a path with no page name at all,
# the same situation as a blank path (`ref.missing_path`). The closed code
# vocabulary has no dedicated "invalid name" code, and `path.not_markdown` was
# misleading here: the file name does end in `.md`, it just names nothing.
BLANK_STEM_PATHS = [
    pytest.param(".md", id="empty"),
    pytest.param(".MD", id="empty-upper"),
    pytest.param(".Md", id="empty-mixed"),
    pytest.param(" .md", id="space"),
    pytest.param("  .md", id="two-spaces"),
    pytest.param("\t.md", id="tab"),
    pytest.param("\u00a0.md", id="nbsp"),
    pytest.param("\u2003.md", id="em-space"),
]


@pytest.mark.parametrize("path", BLANK_STEM_PATHS)
def test_page_with_an_empty_stem_has_no_page_name(path: str) -> None:
    error = failure(path_ref(path))

    assert error.code == "ref.missing_path"
    assert "before '.md'" in error.hint
    assert MESSAGE_SHAPE.match(str(error))


def test_the_blank_stem_cases_are_all_distinct() -> None:
    # Guards the parametrization itself: a visually identical (NBSP vs space)
    # duplicate would silently halve the coverage.
    values = [param.values[0] for param in BLANK_STEM_PATHS]
    assert len(set(values)) == len(values)


@pytest.mark.parametrize("path", ["a.md", "..md", ".md.md", "-.md", "my page.md", "Ünï cödé.md"])
def test_page_with_a_non_blank_stem_stays_accepted(path: str) -> None:
    assert validate_page_ref(path_ref(path)) == path


# -- unsafe flat names: control characters and surrounding whitespace -----------

UNSAFE_NAMES = [
    pytest.param("a\x00b.md", "control characters", id="nul"),
    pytest.param("a\nb.md", "control characters", id="newline"),
    pytest.param("a\tb.md", "control characters", id="tab"),
    pytest.param("a\rb.md", "control characters", id="carriage-return"),
    pytest.param("a\x1bb.md", "control characters", id="escape"),
    pytest.param("a\x7fb.md", "control characters", id="del"),
    pytest.param("a\x85b.md", "control characters", id="c1-nel"),
    pytest.param("a\x00", "control characters", id="nul-without-suffix"),
    pytest.param(" a.md", "whitespace", id="leading-space"),
    pytest.param("\u00a0a.md", "whitespace", id="leading-nbsp"),
    pytest.param("a .md", "whitespace", id="space-before-suffix"),
    pytest.param("a\u2003.md", "whitespace", id="em-space-before-suffix"),
    pytest.param("a.md ", "whitespace", id="trailing-space"),
    pytest.param("a.txt\u00a0", "whitespace", id="trailing-nbsp-without-suffix"),
]


@pytest.mark.parametrize(("path", "reason"), UNSAFE_NAMES)
def test_unsafe_flat_names_are_rejected(path: str, reason: str) -> None:
    error = failure(path_ref(path))

    assert error.code == "path.reserved"
    assert reason in error.summary
    assert MESSAGE_SHAPE.match(str(error))
    assert "\n" not in str(error) and "\x00" not in str(error)


def test_unsafe_names_are_reported_before_the_markdown_suffix_rule() -> None:
    assert failure(path_ref("notes\x00.txt")).code == "path.reserved"
    assert failure(path_ref("notes.txt ")).code == "path.reserved"


@pytest.mark.parametrize("path", ["a\x00/b.md", "a/\x00.md"])
def test_nested_is_checked_before_unsafe_names(path: str) -> None:
    assert failure(path_ref(path)).code == "path.nested_not_supported"


def test_reserved_git_is_checked_before_unsafe_names() -> None:
    assert failure(path_ref(".git/\x00x.md")).code == "path.reserved"
    assert failure(path_ref("a/.git/\x00")).code == "path.reserved"


# -- check order: kind -> missing -> reserved -> nested -> blank stem -> unsafe -> not_markdown --


def test_kind_is_checked_before_everything_else() -> None:
    assert failure(path_ref(".git/x.txt", kind=RefKind.ALIAS)).code == "ref.unsupported_kind"


def test_missing_path_is_checked_before_path_rules() -> None:
    assert failure(path_ref(None)).code == "ref.missing_path"


def test_reserved_is_checked_before_nested_and_markdown() -> None:
    assert failure(path_ref("a/.git/x.txt")).code == "path.reserved"


def test_nested_is_checked_before_markdown() -> None:
    assert failure(path_ref("a/b.txt")).code == "path.nested_not_supported"


# -- message shape (GW-P12) ---------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        path_ref("Home.md", kind=RefKind.ID),
        path_ref(None),
        path_ref(".git/config"),
        path_ref("a/b.md"),
        path_ref("notes.txt"),
    ],
)
def test_every_ref_failure_follows_the_message_shape(ref: DocumentRef) -> None:
    error = failure(ref)

    assert isinstance(error, ConfigurationError)
    assert MESSAGE_SHAPE.match(str(error))
    assert "\n" not in str(error)


# -- derive_root_ref (ref-less create and child-create) ------------------------


@pytest.mark.parametrize(
    ("title", "name"),
    [
        ("Home", "Home.md"),
        ("Getting Started", "Getting-Started.md"),
        ("  Home  ", "Home.md"),
        ("a   b\tc", "a-b-c.md"),
        ("Notes.md", "Notes.md"),
        ("NOTES.MD", "NOTES.MD"),
        ("v1.2 notes", "v1.2-notes.md"),
        ("Ünï cødé", "Ünï-cødé.md"),
        ("x .md", "x-.md"),
        ("_Sidebar", "_Sidebar.md"),
    ],
)
def test_title_derives_a_flat_root_page_reference(title: str, name: str) -> None:
    ref = derive_root_ref(title, provider="wiki")

    assert ref.provider == "wiki"
    assert ref.kind is RefKind.PATH
    assert ref.locator == {"path": name}
    assert ref.title_hint == title


@pytest.mark.parametrize("title", ["Getting Started", "x.md", "Ünï cødé", "NOTES.MD"])
def test_derived_reference_is_always_a_valid_flat_page(title: str) -> None:
    ref = derive_root_ref(title, provider="wiki")

    assert validate_page_ref(ref) == ref.locator["path"]
    assert "/" not in ref.locator["path"]


@pytest.mark.parametrize(
    "title",
    [
        "",
        "   ",
        "\t\n",
        "guides/setup",
        "a\\b",
        "a:b",
        "a*b",
        'say "hi"',
        "a<b>",
        "a|b",
        "a?b",
        ".hidden",
        ".",
        "..",
        ".git",
        "bad\x00name",
        "bell\x07",
        "del\x7f",
        "c1\x80name",
        "c1\x9fname",
    ],
)
def test_unusable_title_is_rejected_and_never_nests(title: str) -> None:
    with pytest.raises(GithubWikiError) as caught:
        derive_root_ref(title, provider="wiki")

    assert caught.value.code == "title.invalid"
    assert MESSAGE_SHAPE.match(str(caught.value))
    assert "\n" not in str(caught.value)


def test_title_problem_is_named_in_the_error_context() -> None:
    with pytest.raises(GithubWikiError) as caught:
        derive_root_ref("guides/setup", provider="wiki")

    assert caught.value.context["title"] == "guides/setup"
    assert "'/'" in caught.value.summary
