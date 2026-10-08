"""Unit tests for ``EntryPointBackendResolver`` (GW-P7, GW-LB1, design D4).

The resolver turns ``local_backend`` settings into a ``DocumentProvider`` rooted
at the clone: it validates the selection offline (no filesystem write, no
provider instantiation), injects ``root`` and ``provider_name`` when creating,
and rejects backends that cannot serve a wiki. Backends come from
``manager_with`` (no entry points): the real ``local_files`` and the fake.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import Field
from wikiops_sdk.contracts import ProviderSettings
from wikiops_sdk.domain import ProviderCapability

from tests.support.fake_file_backend import (
    FakeFileBackend,
    FakeFileBackendFactory,
    FakeFileBackendSettings,
    manager_with,
)
from wikiops.providers.github_wiki.backend import (
    REQUIRED_BACKEND_CAPS,
    EntryPointBackendResolver,
)
from wikiops.providers.github_wiki.errors import GithubWikiError
from wikiops.providers.github_wiki.ports import BackendResolver
from wikiops.providers.github_wiki.settings import LocalBackendSettings
from wikiops.providers.local_files.provider import LocalFilesProviderFactory

C = ProviderCapability


def selection(type_: str = "local_files", **options: object) -> LocalBackendSettings:
    return LocalBackendSettings(type=type_, **options)


class RootlessSettings(ProviderSettings):
    endpoint: str = "https://example.test"


class RootlessBackend(FakeFileBackend):
    pass


class RootlessFactory:
    provider_id = "rootless"
    settings_model = RootlessSettings

    def create(self, settings: RootlessSettings) -> RootlessBackend:
        return RootlessBackend(FakeFileBackendSettings(provider_name=settings.provider_name, root="."))


class NoModelFactory:
    provider_id = "no_model"
    settings_model = None

    def create(self, settings: dict) -> FakeFileBackend:
        return FakeFileBackend(FakeFileBackendSettings.model_validate(settings))


class LimitedFactory(FakeFileBackendFactory):
    """A fake backend that lacks some required capabilities."""

    lacking: frozenset[ProviderCapability] = frozenset()

    def create(self, settings):
        factory = self

        class Limited(FakeFileBackend):
            def capabilities(self) -> set[ProviderCapability]:
                return super().capabilities() - factory.lacking

        typed = FakeFileBackendSettings.model_validate(settings) if isinstance(settings, dict) else settings
        return Limited(typed)


def limited(provider_id: str, *lacking: ProviderCapability) -> LimitedFactory:
    factory = LimitedFactory()
    factory.provider_id = provider_id
    factory.lacking = frozenset(lacking)
    return factory


class ExplodingValidateSettings(ProviderSettings):
    root: str = Field(..., min_length=1)


class ExplodingBackend(FakeFileBackend):
    def validate_settings(self) -> None:
        raise ValueError("cannot reach https://user:p@ss@host.example/repo")


class ExplodingFactory:
    provider_id = "exploding"
    settings_model = FakeFileBackendSettings

    def create(self, settings: FakeFileBackendSettings) -> ExplodingBackend:
        return ExplodingBackend(settings)


class CodedFailureBackend(FakeFileBackend):
    """A backend whose own validation raises a github_wiki-coded error (not ours to re-raise)."""

    def validate_settings(self) -> None:
        raise GithubWikiError("sync.git_failed", "the backend ran git and it failed")


class CodedValidateFactory:
    provider_id = "coded_validate"
    settings_model = FakeFileBackendSettings

    def create(self, settings: FakeFileBackendSettings) -> CodedFailureBackend:
        return CodedFailureBackend(settings)


class CodedCreateFactory:
    provider_id = "coded_create"
    settings_model = FakeFileBackendSettings

    def create(self, settings: FakeFileBackendSettings) -> FakeFileBackend:
        raise GithubWikiError("auth.rejected", "the backend factory failed to authenticate")


class SpyFactory(FakeFileBackendFactory):
    provider_id = "spy"

    def __init__(self) -> None:
        self.created: list[object] = []

    def create(self, settings):
        self.created.append(settings)
        return super().create(settings)


@pytest.fixture
def resolver() -> EntryPointBackendResolver:
    return EntryPointBackendResolver(
        manager_with(
            LocalFilesProviderFactory(),
            FakeFileBackendFactory(),
            RootlessFactory(),
            NoModelFactory(),
            limited("no_assets", C.PUT_ASSET),
            limited("no_read_no_assets", C.PUT_ASSET, C.READ_DOCUMENT, C.RESOLVE_BY_PATH),
            ExplodingFactory(),
            CodedValidateFactory(),
            CodedCreateFactory(),
        )
    )


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    path = tmp_path / "clone"
    path.mkdir()
    return path


def failure(call) -> GithubWikiError:
    with pytest.raises(GithubWikiError) as caught:
        call()
    return caught.value


# -- the required capability set --------------------------------------------------


def test_the_required_capabilities_are_the_six_a_wiki_backend_needs() -> None:
    assert set(REQUIRED_BACKEND_CAPS) == {
        C.READ_DOCUMENT,
        C.CHECK_EXISTS,
        C.CREATE_DOCUMENT,
        C.UPDATE_DOCUMENT,
        C.PUT_ASSET,
        C.RESOLVE_BY_PATH,
    }
    assert len(REQUIRED_BACKEND_CAPS) == 6


def test_the_resolver_satisfies_the_backend_resolver_port(resolver: EntryPointBackendResolver) -> None:
    port: BackendResolver = resolver

    assert callable(port.check_offline) and callable(port.create)


# -- check_offline: selection --------------------------------------------------------


@pytest.mark.parametrize("type_", ["local_files", "fake_files", "spy"])
def test_check_offline_accepts_a_registered_backend_that_takes_a_root(type_: str) -> None:
    resolver = EntryPointBackendResolver(
        manager_with(LocalFilesProviderFactory(), FakeFileBackendFactory(), SpyFactory())
    )

    resolver.check_offline(selection(type_))


def test_check_offline_names_the_registered_ids_for_an_unknown_type(
    resolver: EntryPointBackendResolver,
) -> None:
    error = failure(lambda: resolver.check_offline(selection("missing_backend")))

    assert error.code == "config.backend_unknown"
    assert error.context["type"] == "missing_backend"
    for known in ("fake_files", "local_files", "rootless"):
        assert known in error.hint
    assert error.hint.index("exploding") < error.hint.index("local_files")  # sorted listing


@pytest.mark.parametrize("type_", ["rootless", "no_model"])
def test_check_offline_rejects_a_backend_whose_settings_have_no_root(
    resolver: EntryPointBackendResolver, type_: str
) -> None:
    error = failure(lambda: resolver.check_offline(selection(type_)))

    assert error.code == "config.backend_invalid"
    assert "root" in error.hint
    assert error.context["type"] == type_


# -- check_offline: backend options ---------------------------------------------------


@pytest.mark.parametrize(
    ("options", "fragment"),
    [
        ({"overwrite_existing": "maybe"}, "overwrite_existing"),
        ({"bogus_option": 1}, "bogus_option"),
        ({"assets_dir": 7}, "assets_dir"),
    ],
    ids=["bad-bool", "unknown-option", "bad-type"],
)
def test_check_offline_rejects_invalid_backend_options_with_the_backend_message(
    resolver: EntryPointBackendResolver, options: dict, fragment: str
) -> None:
    error = failure(lambda: resolver.check_offline(selection("local_files", **options)))

    assert error.code == "config.backend_invalid"
    assert fragment in str(error)
    assert error.context["type"] == "local_files"


def test_check_offline_accepts_valid_passthrough_options(
    resolver: EntryPointBackendResolver,
) -> None:
    resolver.check_offline(selection("local_files", assets_dir="media", overwrite_existing=True))


def test_check_offline_writes_nothing_and_instantiates_no_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = SpyFactory()
    resolver = EntryPointBackendResolver(manager_with(LocalFilesProviderFactory(), spy))
    monkeypatch.chdir(tmp_path)
    before = sorted(path.name for path in tmp_path.rglob("*"))

    resolver.check_offline(selection("spy"))
    resolver.check_offline(selection("local_files", assets_dir="media"))

    assert spy.created == []
    assert sorted(path.name for path in tmp_path.rglob("*")) == before


# -- create ---------------------------------------------------------------------------


def test_create_injects_the_workdir_as_root_and_the_wrapper_provider_name(
    resolver: EntryPointBackendResolver, workdir: Path
) -> None:
    backend = resolver.create(selection("fake_files"), root=workdir, provider_name="team-wiki")

    assert isinstance(backend, FakeFileBackend)
    assert backend.settings.root == str(workdir)
    assert backend.settings.provider_name == "team-wiki"


def test_create_passes_backend_options_through_to_local_files(
    resolver: EntryPointBackendResolver, workdir: Path
) -> None:
    backend = resolver.create(
        selection("local_files", assets_dir="media", overwrite_existing=True),
        root=workdir,
        provider_name="wiki",
    )

    assert backend.settings.assets_dir == "media"
    assert backend.settings.overwrite_existing is True
    assert backend.settings.root == str(workdir)
    assert backend.settings.provider_name == "wiki"


def test_create_does_not_pass_the_selector_itself_as_a_backend_option(workdir: Path) -> None:
    spy = SpyFactory()
    resolver = EntryPointBackendResolver(manager_with(spy))

    resolver.create(selection("spy", assets_dir="media"), root=workdir, provider_name="wiki")

    (settings,) = spy.created
    assert settings.assets_dir == "media"
    assert not hasattr(settings, "type")


def test_create_returns_a_working_backend_rooted_at_the_clone(
    resolver: EntryPointBackendResolver, workdir: Path
) -> None:
    backend = resolver.create(selection(), root=workdir, provider_name="wiki")
    (workdir / "Home.md").write_text("# Home\n", encoding="utf-8")

    from wikiops_sdk.domain import DocumentRef, RefKind

    ref = DocumentRef(provider="wiki", kind=RefKind.PATH, locator={"path": "Home.md"})
    assert backend.exists(ref) is True
    assert backend.get_document(ref).content == "# Home\n"


# -- create: errors ---------------------------------------------------------------------


def test_create_rejects_a_backend_lacking_a_required_capability_naming_it(
    resolver: EntryPointBackendResolver, workdir: Path
) -> None:
    error = failure(lambda: resolver.create(selection("no_assets"), root=workdir, provider_name="w"))

    assert error.code == "config.backend_incompatible"
    assert "PUT_ASSET" in str(error)
    assert error.context["missing"] == "PUT_ASSET"


def test_create_lists_every_missing_capability_in_the_canonical_order(
    resolver: EntryPointBackendResolver, workdir: Path
) -> None:
    error = failure(
        lambda: resolver.create(selection("no_read_no_assets"), root=workdir, provider_name="w")
    )

    assert error.code == "config.backend_incompatible"
    assert error.context["missing"] == "READ_DOCUMENT, PUT_ASSET, RESOLVE_BY_PATH"


def test_create_reports_an_unregistered_backend_as_unknown(
    resolver: EntryPointBackendResolver, workdir: Path
) -> None:
    error = failure(lambda: resolver.create(selection("nope"), root=workdir, provider_name="w"))

    assert error.code == "config.backend_unknown"


def test_create_rejects_a_missing_root_with_the_backend_message(
    resolver: EntryPointBackendResolver, tmp_path: Path
) -> None:
    error = failure(
        lambda: resolver.create(selection(), root=tmp_path / "not-there", provider_name="w")
    )

    assert error.code == "config.backend_invalid"
    assert "[local_files:" in str(error)  # the backend's own coded message is carried through


def test_create_rejects_option_values_the_backend_refuses(
    resolver: EntryPointBackendResolver, workdir: Path
) -> None:
    error = failure(
        lambda: resolver.create(
            selection("local_files", assets_dir="../outside"), root=workdir, provider_name="w"
        )
    )

    assert error.code == "config.backend_invalid"


def test_create_rejects_invalid_options_even_without_the_offline_check(
    resolver: EntryPointBackendResolver, workdir: Path
) -> None:
    error = failure(
        lambda: resolver.create(
            selection("local_files", bogus_option=1), root=workdir, provider_name="w"
        )
    )

    assert error.code == "config.backend_invalid"
    assert "bogus_option" in str(error)
    assert "Extra inputs are not permitted" in str(error)


def test_create_redacts_secrets_in_a_backend_failure(
    resolver: EntryPointBackendResolver, workdir: Path
) -> None:
    error = failure(lambda: resolver.create(selection("exploding"), root=workdir, provider_name="w"))

    rendered = str(error)
    assert error.code == "config.backend_invalid"
    assert "p@ss" not in rendered and "user:" not in rendered
    assert "***@host.example" in rendered


@pytest.mark.parametrize(
    ("backend_type", "original_code"),
    [("coded_validate", "sync.git_failed"), ("coded_create", "auth.rejected")],
)
def test_a_github_wiki_error_raised_by_the_backend_is_wrapped_not_propagated(
    resolver: EntryPointBackendResolver, workdir: Path, backend_type: str, original_code: str
) -> None:
    error = failure(
        lambda: resolver.create(selection(backend_type), root=workdir, provider_name="w")
    )

    assert error.code == "config.backend_invalid"  # not the backend's own code
    assert error.context["type"] == backend_type
    assert "could not be created" in error.summary
    assert f"[github_wiki:{original_code}]" in error.summary  # its message is carried, redacted


def test_the_resolvers_own_coded_errors_are_still_raised_unchanged(
    resolver: EntryPointBackendResolver, workdir: Path
) -> None:
    unknown = failure(lambda: resolver.create(selection("nope"), root=workdir, provider_name="w"))
    options = failure(
        lambda: resolver.create(selection(bogus_option=1), root=workdir, provider_name="w")
    )
    incompatible = failure(
        lambda: resolver.create(selection("no_assets"), root=workdir, provider_name="w")
    )

    assert unknown.code == "config.backend_unknown"
    assert options.code == "config.backend_invalid" and "could not be created" not in options.summary
    assert incompatible.code == "config.backend_incompatible"


@pytest.mark.parametrize(
    "options",
    [{"bogus_option": 1}, {"overwrite_existing": "maybe"}, {"assets_dir": 7}],
    ids=["unknown-option", "bad-bool", "bad-type"],
)
def test_the_offline_check_and_create_report_invalid_options_identically(
    resolver: EntryPointBackendResolver, workdir: Path, options: dict
) -> None:
    offline = failure(lambda: resolver.check_offline(selection("local_files", **options)))
    created = failure(
        lambda: resolver.create(
            selection("local_files", **options), root=workdir, provider_name="w"
        )
    )

    assert (offline.code, offline.summary, offline.context) == (
        created.code,
        created.summary,
        created.context,
    )
