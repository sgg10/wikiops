"""Unit tests for the asset-reference and link policy of ``github_wiki``.

Covers GW-P10 / GW-LB4: document-relative asset references, the link guard
(no root-anchored links, no raw-content URLs) and the GitHub wiki page URL.
"""

from __future__ import annotations

import inspect
import re

import pytest
from wikiops_sdk.domain import AssetRef, AssetRefKind, DocumentRef, RefKind

from wikiops.providers.github_wiki import layout
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.layout import (
    build_asset_reference,
    build_link,
    guard_link,
)

MESSAGE_SHAPE = re.compile(r"^\[github_wiki:[a-z_]+\.[a-z_]+\] .+ Hint: .+\.$")


def asset_ref(
    path: str | None, *, kind: AssetRefKind = AssetRefKind.PATH
) -> AssetRef:
    locator = {} if path is None else {"path": path}
    return AssetRef(provider="wiki", kind=kind, locator=locator)


def page_ref(path: str, *, kind: RefKind = RefKind.PATH) -> DocumentRef:
    return DocumentRef(provider="wiki", kind=kind, locator={"path": path})


# -- build_asset_reference ----------------------------------------------------


@pytest.mark.parametrize(
    ("path", "reference"),
    [
        ("assets/diagram--0123456789abcdef.png", "assets/diagram--0123456789abcdef.png"),
        ("media/img/x.png", "media/img/x.png"),
        ("assets/my diagram.png", "assets/my%20diagram.png"),
        ("assets/é.png", "assets/%C3%A9.png"),
        ("assets/100%.png", "assets/100%25.png"),
        ("assets/a#b.png", "assets/a%23b.png"),
    ],
)
def test_asset_reference_is_document_relative_and_quoted(
    path: str, reference: str
) -> None:
    result = build_asset_reference(asset_ref(path))

    assert result == reference
    assert not result.startswith("/")
    assert "://" not in result


@pytest.mark.parametrize("kind", [AssetRefKind.ID, AssetRefKind.URL])
def test_non_path_asset_ref_is_unsupported(kind: AssetRefKind) -> None:
    with pytest.raises(GithubWikiError) as caught:
        build_asset_reference(asset_ref("assets/x.png", kind=kind))

    assert caught.value.code == "asset.ref_unsupported"
    assert caught.value.context["kind"] == kind.value


def test_path_asset_ref_without_a_path_is_unsupported() -> None:
    with pytest.raises(GithubWikiError) as caught:
        build_asset_reference(asset_ref(None))

    assert caught.value.code == "asset.ref_unsupported"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/assets/x.png",
        "./assets/x.png",
        "assets//x.png",
        "../x.png",
        "assets/../x.png",
        ".git/x.png",
        "assets/.git/x.png",
        "C:/assets/x.png",
    ],
)
def test_non_canonical_asset_path_is_unsupported(path: str) -> None:
    with pytest.raises(GithubWikiError) as caught:
        build_asset_reference(asset_ref(path))

    assert caught.value.code == "asset.ref_unsupported"
    assert caught.value.context["path"] == path
    assert MESSAGE_SHAPE.match(str(caught.value))


# -- guard_link ---------------------------------------------------------------


@pytest.mark.parametrize(
    "link",
    [
        "/assets/x.png",
        "//cdn.example/x.png",
        "/owner/repo/wiki/Home",
        "  /assets/x.png",
    ],
)
def test_root_anchored_link_is_rejected(link: str) -> None:
    with pytest.raises(GithubWikiError) as caught:
        guard_link(link)

    assert caught.value.code == "link.root_anchored"
    assert MESSAGE_SHAPE.match(str(caught.value))


@pytest.mark.parametrize(
    "link",
    [
        "https://raw.githubusercontent.com/wiki/acme/platform/assets/x.png",
        "http://raw.githubusercontent.com/wiki/acme/platform/x.png",
        "HTTPS://RAW.GITHUBUSERCONTENT.COM/wiki/acme/platform/x.png",
        "https://raw.githubusercontent.com",
        "https://raw.githubusercontent.com:443/x.png",
        " https://raw.githubusercontent.com/x.png",
    ],
)
def test_raw_content_url_is_rejected(link: str) -> None:
    with pytest.raises(GithubWikiError) as caught:
        guard_link(link)

    assert caught.value.code == "link.raw_url"
    assert MESSAGE_SHAPE.match(str(caught.value))


@pytest.mark.parametrize(
    "link",
    [
        "assets/x.png",
        "Home",
        "guides-setup",
        "https://github.com/acme/platform/wiki/Home",
        "https://ghe.acme.io/acme/platform/wiki/Home",
        "https://example.com/raw.githubusercontent.com/x",
        "https://raw.githubusercontent.com.evil.example/x",
        "",
    ],
)
def test_relative_and_ordinary_https_links_are_accepted_unchanged(link: str) -> None:
    assert guard_link(link) == link


# -- build_link ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "path", "url"),
    [
        ("github.com", "Home.md", "https://github.com/acme/platform/wiki/Home"),
        ("ghe.acme.io", "Home.md", "https://ghe.acme.io/acme/platform/wiki/Home"),
        (
            "github.com",
            "guides-setup.md",
            "https://github.com/acme/platform/wiki/guides-setup",
        ),
        ("github.com", "My Page.md", "https://github.com/acme/platform/wiki/My%20Page"),
        ("github.com", "Notes.MD", "https://github.com/acme/platform/wiki/Notes"),
        ("github.com", "100%.md", "https://github.com/acme/platform/wiki/100%25"),
        ("github.com", "Home.md.md", "https://github.com/acme/platform/wiki/Home.md"),
    ],
)
def test_page_link_is_the_wiki_url_of_the_page_stem(
    host: str, path: str, url: str
) -> None:
    link = build_link(page_ref(path), host=host, repository="acme/platform")

    assert link == url
    assert guard_link(link) == link


def test_page_link_applies_the_page_policy_first() -> None:
    with pytest.raises(GithubWikiError) as caught:
        build_link(page_ref("guides/setup.md"), host="github.com", repository="acme/platform")

    assert caught.value.code == "path.nested_not_supported"


@pytest.mark.parametrize("path", [".md", ".MD", " .md", "\u00a0.md", "\t.md"])
def test_page_link_never_points_at_the_wiki_root_for_an_empty_stem(path: str) -> None:
    with pytest.raises(GithubWikiError) as caught:
        build_link(page_ref(path), host="github.com", repository="acme/platform")

    assert caught.value.code == "ref.missing_path"


@pytest.mark.parametrize("path", ["a\x00b.md", " a.md", "a .md", "a\nb.md"])
def test_page_link_is_never_built_for_an_unsafe_name(path: str) -> None:
    with pytest.raises(GithubWikiError) as caught:
        build_link(page_ref(path), host="github.com", repository="acme/platform")

    assert caught.value.code == "path.reserved"


@pytest.mark.parametrize(
    "host",
    [
        "",
        "https://github.com",
        "user@github.com",
        "github.com/path",
        "github.com:443",
        "-oProxyCommand=x",
        "git hub.com",
        "github.com\n.evil",
    ],
)
def test_page_link_rejects_a_malformed_host(host: str) -> None:
    with pytest.raises(GithubWikiError) as caught:
        build_link(page_ref("Home.md"), host=host, repository="acme/platform")

    assert caught.value.code == "config.invalid_host"
    assert MESSAGE_SHAPE.match(str(caught.value))


@pytest.mark.parametrize(
    "repository",
    [
        "",
        "platform",
        "acme/platform/extra",
        "acme/platform.wiki",
        "acme/platform.git",
        "acme/..",
        "https://github.com/acme/platform",
        "user:pw@acme/platform",
        "acme/plat form",
        "acme/platform?x=1",
        "../acme/platform",
    ],
)
def test_page_link_rejects_a_malformed_repository(repository: str) -> None:
    with pytest.raises(GithubWikiError) as caught:
        build_link(page_ref("Home.md"), host="github.com", repository=repository)

    assert caught.value.code == "config.invalid_repository"
    assert MESSAGE_SHAPE.match(str(caught.value))


def test_page_policy_is_checked_before_host_and_repository() -> None:
    with pytest.raises(GithubWikiError) as caught:
        build_link(page_ref("a/b.md"), host="", repository="")

    assert caught.value.code == "path.nested_not_supported"


def test_page_link_rejects_non_path_refs() -> None:
    with pytest.raises(GithubWikiError) as caught:
        build_link(
            page_ref("Home.md", kind=RefKind.ALIAS),
            host="github.com",
            repository="acme/platform",
        )

    assert caught.value.code == "ref.unsupported_kind"


# -- plugin content is never touched ------------------------------------------


def test_layout_exposes_no_api_that_receives_page_content() -> None:
    public = [
        obj
        for name, obj in vars(layout).items()
        if not name.startswith("_")
        and inspect.isfunction(obj)
        and obj.__module__ == layout.__name__
    ]

    assert {func.__name__ for func in public} >= {
        "validate_page_ref",
        "derive_root_ref",
        "build_asset_reference",
        "guard_link",
        "build_link",
    }
    content_like = {"content", "text", "body", "markdown", "new_content"}
    for func in public:
        assert not content_like & set(inspect.signature(func).parameters), func.__name__
