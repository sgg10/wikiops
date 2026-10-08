"""The read side of ``GithubWikiProvider`` (GW-P1 class, P6, P7, P8 read, P9, P10).

Everything runs over fakes: a ``FakeGitRunner`` answered by ``FakeWikiGit`` and a
backend resolver over the real ``local_files`` backend and the independent
``fake_files`` one. Offline construction and validation, the static capability
set, the page policy at ``resolve_ref``, and reads that sync first and then
delegate unchanged to the lazily created backend are covered here.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from wikiops_sdk.contracts import ProviderSettings
from wikiops_sdk.domain import AssetRef, AssetRefKind, DocumentRef, ProviderCapability, RefKind

from tests.support.fake_file_backend import FakeFileBackend, FakeFileBackendFactory, manager_with
from tests.support.provider_harness import ProviderHarness, build_provider
from wikiops.providers.github_wiki.backend import EntryPointBackendResolver
from wikiops.providers.github_wiki.errors import GithubWikiError

C = ProviderCapability
STATIC_CAPABILITIES = {
    C.READ_DOCUMENT,
    C.CHECK_EXISTS,
    C.CREATE_DOCUMENT,
    C.UPDATE_DOCUMENT,
    C.CREATE_CHILD_DOCUMENT,
    C.BUILD_LINK,
    C.PUT_ASSET,
    C.RESOLVE_BY_PATH,
    C.VERSION_CHECK,
}


def page(path: str | None = "Home.md", *, provider: str = "", kind: RefKind = RefKind.PATH) -> DocumentRef:
    locator = {} if path is None else {"path": path}
    return DocumentRef(provider=provider, kind=kind, locator=locator)


def failure(action: Callable[[], object]) -> GithubWikiError:
    with pytest.raises(GithubWikiError) as caught:
        action()
    return caught.value


def message_of(action: Callable[[], object]) -> str:
    with pytest.raises(Exception) as caught:  # noqa: PT011 - backend errors are a foreign type
        action()
    assert not isinstance(caught.value, GithubWikiError), "backend errors must not be rewritten"
    return str(caught.value)


def write_page(harness: ProviderHarness, name: str, content: str) -> None:
    (harness.workdir / name).write_text(content, encoding="utf-8")


@pytest.fixture
def clone(tmp_path: Path) -> ProviderHarness:
    return build_provider(tmp_path, cloned=True)


# -- identity and offline construction ----------------------------------------------------


def test_the_provider_identifies_itself_and_exposes_provider_settings(tmp_path: Path) -> None:
    harness = build_provider(tmp_path)

    assert harness.provider.provider_id == "github_wiki"
    assert harness.provider.settings is harness.settings
    assert isinstance(harness.provider.settings, ProviderSettings)


def test_construction_and_validation_run_no_command_and_write_nothing(tmp_path: Path) -> None:
    harness = build_provider(tmp_path)

    harness.provider.validate_settings()

    assert harness.runner.calls == []
    assert harness.resolver.offline_checks == 1
    assert harness.resolver.created == []
    assert list(tmp_path.iterdir()) == []  # neither the workdir nor its parents exist


def test_validation_does_not_even_resolve_the_workdir(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the workdir parent should be")
    harness = build_provider(tmp_path, workdir=blocker / "wiki")

    harness.provider.validate_settings()  # would raise workdir.unusable if it resolved the path


def test_a_missing_git_executable_is_config_git_unavailable(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, which=lambda name: None)

    error = failure(harness.provider.validate_settings)

    assert error.code == "config.git_unavailable"
    assert harness.runner.calls == []


def test_the_git_check_comes_before_the_credential_check(tmp_path: Path) -> None:
    harness = build_provider(
        tmp_path, settings={"auth": {"mode": "env", "variable": "WIKI_T"}}, which=lambda name: None
    )

    assert failure(harness.provider.validate_settings).code == "config.git_unavailable"


def test_a_missing_env_token_is_reported_offline_without_naming_a_value(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, settings={"auth": {"mode": "env", "variable": "WIKI_T"}}, environ={})

    error = failure(harness.provider.validate_settings)

    assert error.code == "auth.env_missing"
    assert "WIKI_T" in str(error)
    assert harness.runner.calls == []


def test_a_missing_gh_executable_is_reported_by_the_strategy(tmp_path: Path) -> None:
    harness = build_provider(
        tmp_path,
        settings={"auth": {"mode": "gh", "account": "sgg10"}},
        which=lambda name: None if name == "gh" else f"/usr/bin/{name}",
    )

    assert failure(harness.provider.validate_settings).code == "auth.gh_unavailable"
    assert harness.runner.calls == []  # gh was looked up, never run


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        ({"type": "nope"}, "config.backend_unknown"),
        ({"type": "local_files", "assets_dir": 5}, "config.backend_invalid"),
    ],
)
def test_backend_selection_errors_surface_from_validation(
    tmp_path: Path, backend: dict[str, object], expected: str
) -> None:
    harness = build_provider(tmp_path, settings={"local_backend": backend})

    assert failure(harness.provider.validate_settings).code == expected
    assert harness.resolver.created == []


# -- capabilities (gap G1) ------------------------------------------------------------------


def test_capabilities_are_the_static_set_before_any_backend_exists(tmp_path: Path) -> None:
    harness = build_provider(tmp_path)

    capabilities = harness.provider.capabilities()

    assert capabilities == STATIC_CAPABILITIES
    assert C.HIERARCHICAL_PAGES not in capabilities
    assert harness.runner.calls == []
    assert harness.resolver.created == []


def test_capabilities_do_not_depend_on_the_backend_and_hand_out_a_fresh_set(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, settings={"local_backend": {"type": "fake_files"}})

    first = harness.provider.capabilities()
    first.clear()

    assert harness.provider.capabilities() == STATIC_CAPABILITIES


# -- resolve_ref: page policy first, then the plan sync -----------------------------------------


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (page("guides/setup.md"), "path.nested_not_supported"),
        (page(".git/config"), "path.reserved"),
        (page("notes.txt"), "path.not_markdown"),
        (page(None), "ref.missing_path"),
        (page(".md"), "path.invalid_name"),
        (page(" Home.md"), "path.invalid_name"),
        (page("Ho\x00me.md"), "path.invalid_name"),
        (page("Home.md", kind=RefKind.ID), "ref.unsupported_kind"),
    ],
)
def test_resolve_ref_applies_the_page_policy_before_any_command(
    tmp_path: Path, ref: DocumentRef, expected: str
) -> None:
    harness = build_provider(tmp_path, cloned=True)

    error = failure(lambda: harness.provider.resolve_ref(ref, None))

    assert error.code == expected
    assert harness.runner.calls == []  # a plan fails early, without touching git
    assert harness.resolver.created == []


def test_resolve_ref_syncs_for_plan_then_delegates_and_fills_the_provider(clone: ProviderHarness) -> None:
    resolved = clone.provider.resolve_ref(page("Home.md"))

    assert resolved.provider == "docs"
    assert resolved.locator == {"path": "Home.md"}
    assert {"ls-remote", "fetch"} <= set(clone.network_calls())
    assert "merge" in clone.git_subcommands()


def test_resolve_ref_keeps_a_provider_that_is_already_set(clone: ProviderHarness) -> None:
    assert clone.provider.resolve_ref(page("Home.md", provider="other")).provider == "other"


def test_the_sync_runs_once_per_provider_instance(clone: ProviderHarness) -> None:
    write_page(clone, "Home.md", "# Home\n")
    clone.provider.resolve_ref(page("Home.md"))
    seen = len(clone.runner.calls)
    assert seen > 0

    clone.provider.resolve_ref(page("Other.md"))
    clone.provider.exists(page("Home.md"))
    clone.provider.get_document(page("Home.md"))

    assert len(clone.runner.calls) == seen  # no second ls-remote/fetch/status


def test_a_foreign_dirty_path_fails_the_plan_at_resolve_ref(clone: ProviderHarness) -> None:
    clone.fake.dirty = ["?? notes.txt"]

    error = failure(lambda: clone.provider.resolve_ref(page("Home.md")))

    assert error.code == "workdir.dirty"
    assert clone.resolver.created == []


def test_without_a_clone_the_first_read_clones_and_only_then_creates_the_backend(tmp_path: Path) -> None:
    harness = build_provider(tmp_path)
    assert harness.resolver.created == []

    harness.provider.resolve_ref(page("Home.md"))

    subcommands = harness.git_subcommands()
    assert subcommands.index("ls-remote") < subcommands.index("clone")
    assert harness.resolver.created == [(harness.workdir, "docs", True)]  # the workdir existed already


def test_the_backend_is_created_once_with_the_workdir_as_root(clone: ProviderHarness) -> None:
    clone.provider.resolve_ref(page("Home.md"))
    clone.provider.exists(page("Home.md"))

    assert clone.resolver.created == [(clone.workdir, "docs", True)]


def test_a_failed_sync_creates_no_backend_and_is_not_cached(clone: ProviderHarness) -> None:
    clone.fake.fail["ls-remote"] = (128, "fatal: Authentication failed for 'https://github.com/acme/platform.wiki.git/'")

    error = failure(lambda: clone.provider.resolve_ref(page("Home.md")))
    assert error.code == "auth.rejected"
    assert clone.resolver.created == []

    del clone.fake.fail["ls-remote"]
    assert clone.provider.resolve_ref(page("Home.md")).locator == {"path": "Home.md"}
    assert len(clone.resolver.created) == 1


def test_an_incompatible_backend_is_reported_on_the_first_read(tmp_path: Path) -> None:
    class NoAssets(FakeFileBackend):
        def capabilities(self) -> set[ProviderCapability]:
            return super().capabilities() - {C.PUT_ASSET}

    class Factory(FakeFileBackendFactory):
        def create(self, settings):  # noqa: ANN001, ANN201
            return NoAssets(super().create(settings).settings)

    harness = build_provider(tmp_path, cloned=True, settings={"local_backend": {"type": "fake_files"}})
    harness.resolver.inner = EntryPointBackendResolver(manager_with(Factory()))

    error = failure(lambda: harness.provider.resolve_ref(page("Home.md")))

    assert error.code == "config.backend_incompatible"
    assert "PUT_ASSET" in str(error)


# -- exists / get_document -------------------------------------------------------------------


def test_exists_syncs_then_asks_the_backend(clone: ProviderHarness) -> None:
    write_page(clone, "Home.md", "# Home\n")

    assert clone.provider.exists(page("Home.md")) is True
    assert clone.provider.exists(page("Missing.md")) is False
    assert "ls-remote" in clone.network_calls()


def test_get_document_returns_the_backend_document_unchanged(clone: ProviderHarness) -> None:
    write_page(clone, "Home.md", "# Home\n\nhello\n")
    ref = clone.provider.resolve_ref(page("Home.md"))

    document = clone.provider.get_document(ref)

    assert document.content == "# Home\n\nhello\n"
    assert document.title == "Home"
    assert document.ref == ref
    assert document.version is not None and document.version.token.startswith("sha256:")


def test_a_missing_page_keeps_the_local_files_error_text(clone: ProviderHarness) -> None:
    text = message_of(lambda: clone.provider.get_document(page("Ghost.md")))

    assert text.startswith("[local_files:document.not_found]")
    assert "github_wiki" not in text.split("]")[0]


def test_a_missing_page_keeps_the_error_text_of_a_swapped_backend(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, settings={"local_backend": {"type": "fake_files"}})

    text = message_of(lambda: harness.provider.get_document(page("Ghost.md")))

    assert text.startswith("[fake_files:document.not_found]")


@pytest.mark.parametrize("method", ["exists", "get_document"])
@pytest.mark.parametrize(
    ("path", "expected"),
    [("docs/page.md", "path.nested_not_supported"), (".git/x.md", "path.reserved"), ("a.txt", "path.not_markdown")],
)
def test_reads_apply_the_same_page_policy_before_any_command(
    tmp_path: Path, method: str, path: str, expected: str
) -> None:
    harness = build_provider(tmp_path, cloned=True)

    error = failure(lambda: getattr(harness.provider, method)(page(path)))

    assert error.code == expected
    assert harness.runner.calls == []


# -- offline plans -----------------------------------------------------------------------------


def test_with_sync_on_plan_false_reads_use_the_local_clone_without_network(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, cloned=True, settings={"sync_on_plan": False})
    write_page(harness, "Home.md", "# offline\n")

    document = harness.provider.get_document(harness.provider.resolve_ref(page("Home.md")))

    assert document.content == "# offline\n"
    assert harness.network_calls() == []
    assert {"fetch", "merge", "ls-remote", "clone"}.isdisjoint(harness.git_subcommands())
    assert harness.resolver.created == [(harness.workdir, "docs", True)]


def test_with_sync_on_plan_false_and_no_clone_the_read_fails_without_network(tmp_path: Path) -> None:
    harness = build_provider(tmp_path, settings={"sync_on_plan": False})

    error = failure(lambda: harness.provider.resolve_ref(page("Home.md")))

    assert error.code == "sync.no_local_clone"
    assert harness.runner.calls == []
    assert harness.resolver.created == []


# -- links and asset references --------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "path", "expected"),
    [
        ({}, "Home.md", "https://github.com/acme/platform/wiki/Home"),
        ({"host": "ghe.acme.io"}, "Home.md", "https://ghe.acme.io/acme/platform/wiki/Home"),
        ({}, "My Page.md", "https://github.com/acme/platform/wiki/My%20Page"),
    ],
)
def test_build_link_is_the_wiki_url_and_needs_no_sync(
    tmp_path: Path, settings: dict[str, object], path: str, expected: str
) -> None:
    harness = build_provider(tmp_path, settings=settings)

    assert harness.provider.build_link(page(path)) == expected
    assert harness.runner.calls == []


def test_build_link_applies_the_page_policy(tmp_path: Path) -> None:
    harness = build_provider(tmp_path)

    assert failure(lambda: harness.provider.build_link(page("a/b.md"))).code == "path.nested_not_supported"


def asset(path: str | None, kind: AssetRefKind = AssetRefKind.PATH) -> AssetRef:
    return AssetRef(provider="docs", kind=kind, locator={} if path is None else {"path": path})


@pytest.mark.parametrize(
    ("path", "expected"),
    [("assets/a1b2.png", "assets/a1b2.png"), ("assets/my pic.png", "assets/my%20pic.png")],
)
def test_asset_references_are_document_relative(tmp_path: Path, path: str, expected: str) -> None:
    harness = build_provider(tmp_path)

    assert harness.provider.build_asset_reference(asset(path)) == expected
    assert harness.runner.calls == []


@pytest.mark.parametrize(
    "ref",
    [asset("assets/a.png", AssetRefKind.URL), asset(None), asset("../escape.png")],
)
def test_asset_references_the_wiki_cannot_express_are_refused(tmp_path: Path, ref: AssetRef) -> None:
    harness = build_provider(tmp_path)

    assert failure(lambda: harness.provider.build_asset_reference(ref)).code == "asset.ref_unsupported"
