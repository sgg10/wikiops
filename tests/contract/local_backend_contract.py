"""Behavioral contract of a local backend composable under ``github_wiki``.

``github_wiki`` writes pages and assets into its clone through an inner
``DocumentProvider`` rooted at the workdir (GW-LB1, GW-LB2). This module defines
what any such backend must do; it is not collected itself. A test module
subclasses :class:`LocalBackendContract`, names the backend type and supplies its
factory, and pytest collects every ``test_*`` method for that backend:

    class TestMyBackend(LocalBackendContract):
        backend_type = "my_backend"

        def make_factory(self):
            return MyBackendFactory()

Backends are created exactly as the resolver creates them, through
``ProviderManager.create`` with the ``root`` and ``provider_name`` injected, so
the suite doubles as an admission test. Only root-level pages are exercised:
the wiki is a flat namespace.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote

import pytest
from wikiops_sdk.contracts import DocumentProvider, ProviderSettings
from wikiops_sdk.domain import (
    AppliedOperationResult,
    AssetRefKind,
    ChangeSet,
    CreateChildDocumentOperation,
    CreateDocumentOperation,
    DocumentRef,
    DocumentVersion,
    OperationStatus,
    PluginResourceAssetSource,
    PutAssetOperation,
    RefKind,
    UpdateDocumentOperation,
)

from tests.support.fake_file_backend import manager_with
from wikiops.core.exceptions import ConfigurationError
from wikiops.core.provider_manager import ProviderFactory
from wikiops.providers.github_wiki.backend import REQUIRED_BACKEND_CAPS
from wikiops.providers.github_wiki.layout import build_asset_reference

PROVIDER_NAME = "wiki"
HOME = "Home.md"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
OTHER_PNG = b"\x89PNG\r\n\x1a\n" + bytes(reversed(range(256)))
HOSTILE_PAGE_PATHS = [
    "../escape.md",
    "a/../../escape.md",
    "/absolute-escape.md",
    ".git/config.md",
    "sub/.git/hooks.md",
]
_IMAGE_LINK = re.compile(r"!\[logo\]\(([^)]*)\)")


def page_ref(path: str | None, *, kind: RefKind = RefKind.PATH) -> DocumentRef:
    locator = {} if path is None else {"path": path}
    return DocumentRef(provider=PROVIDER_NAME, kind=kind, locator=locator)


def create_op(path: str, content: str, *, title: str = "Title") -> CreateDocumentOperation:
    return CreateDocumentOperation(ref=page_ref(path), title=title, content=content)


def update_op(
    path: str, content: str, *, expected: DocumentVersion | None = None
) -> UpdateDocumentOperation:
    return UpdateDocumentOperation(ref=page_ref(path), new_content=content, expected_version=expected)


def child_op(parent: str, path: str, content: str) -> CreateChildDocumentOperation:
    return CreateChildDocumentOperation(
        parent_ref=page_ref(parent),
        ref=page_ref(path),
        child_title="Child",
        child_content=content,
    )


def asset_op(name: str) -> PutAssetOperation:
    return PutAssetOperation(
        asset_key="logo",
        source=PluginResourceAssetSource(relative_path="logo.png"),
        name=name,
        media_type="image/png",
    )


def apply_all(backend: DocumentProvider, *operations: object) -> list[AppliedOperationResult]:
    changeset = ChangeSet(plugin_id="contract", operations=list(operations))  # type: ignore[arg-type]
    result = backend.apply_changes(changeset)
    assert [r.operation_id for r in result.results] == [op.operation_id for op in operations]  # type: ignore[attr-defined]
    return result.results


def apply_one(backend: DocumentProvider, operation: object) -> AppliedOperationResult:
    return apply_all(backend, operation)[0]


def version_token(version: DocumentVersion | None) -> str:
    assert version is not None, "the backend returned no version"
    token = version.token or version.etag
    assert token, "the backend returned a version without token or etag"
    return token


def snapshot(base: Path) -> dict[str, bytes | None]:
    """Every file (bytes) and directory (``None``) below ``base``, keyed by relative path."""
    return {
        str(path.relative_to(base)): path.read_bytes() if path.is_file() else None
        for path in sorted(base.rglob("*"))
    }


def assert_canonical_relative(path: str) -> None:
    assert path and not path.startswith("/"), path
    assert "\\" not in path and ".." not in path.split("/"), path
    assert "://" not in path, path


class LocalBackendContract:
    """Subclass, set ``backend_type`` and implement ``make_factory``."""

    backend_type: ClassVar[str]

    def make_factory(self) -> ProviderFactory:
        raise NotImplementedError

    # -- fixtures ------------------------------------------------------------

    @pytest.fixture
    def root(self, tmp_path: Path) -> Path:
        directory = tmp_path / "wiki"
        directory.mkdir()
        return directory

    @pytest.fixture
    def create_backend(self, root: Path) -> Callable[..., DocumentProvider]:
        """Create a backend the way the resolver does, with extra backend options."""

        def create(**options: object) -> DocumentProvider:
            settings = {"provider_name": PROVIDER_NAME, "root": str(root), **options}
            return manager_with(self.make_factory()).create(self.backend_type, settings)

        return create

    @pytest.fixture
    def backend(self, create_backend: Callable[..., DocumentProvider]) -> DocumentProvider:
        return create_backend()

    # -- admission (GW-LB1) --------------------------------------------------

    def test_settings_model_accepts_the_injected_root_and_provider_name(self) -> None:
        model = manager_with(self.make_factory()).settings_model_for(self.backend_type)

        assert model is not None and issubclass(model, ProviderSettings)
        assert {"root", "provider_name"} <= set(model.model_fields)

    def test_backend_supports_every_capability_github_wiki_requires(
        self, backend: DocumentProvider
    ) -> None:
        missing = set(REQUIRED_BACKEND_CAPS) - backend.capabilities()

        assert missing == set()

    def test_creating_and_validating_a_backend_writes_nothing(
        self, create_backend: Callable[..., DocumentProvider], root: Path, tmp_path: Path
    ) -> None:
        before = snapshot(tmp_path)

        backend = create_backend()
        backend.validate_settings()
        backend.capabilities()

        assert snapshot(tmp_path) == before

    # -- pages: create, read, exists ------------------------------------------

    def test_create_writes_a_root_page_and_reports_its_resolved_path(
        self, backend: DocumentProvider, root: Path
    ) -> None:
        result = apply_one(backend, create_op(HOME, "# Home\n\nWelcome.\n"))

        assert result.status is OperationStatus.APPLIED
        assert result.resolved_ref is not None
        assert result.resolved_ref.locator["path"] == HOME
        assert (root / HOME).read_text(encoding="utf-8") == "# Home\n\nWelcome.\n"

    def test_a_created_page_exists_and_reads_back_exactly(self, backend: DocumentProvider) -> None:
        content = "# Título\n\nlínea 1\nlínea 2 ñandú 日本語\n"
        assert backend.exists(page_ref(HOME)) is False

        apply_one(backend, create_op(HOME, content))

        document = backend.get_document(page_ref(HOME))
        assert backend.exists(page_ref(HOME)) is True
        assert document.content == content
        assert document.title == "Home"

    def test_reading_a_missing_page_raises_a_configuration_error(
        self, backend: DocumentProvider
    ) -> None:
        with pytest.raises(ConfigurationError):
            backend.get_document(page_ref("Missing.md"))

    def test_reading_the_same_page_twice_is_idempotent(self, backend: DocumentProvider) -> None:
        apply_one(backend, create_op(HOME, "# Home\n"))

        first = backend.get_document(page_ref(HOME))
        second = backend.get_document(page_ref(HOME))

        assert first.content == second.content
        assert version_token(first.version) == version_token(second.version)

    def test_create_does_not_overwrite_a_page_with_different_content(
        self, backend: DocumentProvider
    ) -> None:
        apply_one(backend, create_op(HOME, "# Original\n"))

        result = apply_one(backend, create_op(HOME, "# Replacement\n"))

        assert result.status is OperationStatus.FAILED
        assert result.message
        assert backend.get_document(page_ref(HOME)).content == "# Original\n"

    def test_create_with_identical_content_is_skipped(self, backend: DocumentProvider) -> None:
        apply_one(backend, create_op(HOME, "# Same\n"))

        result = apply_one(backend, create_op(HOME, "# Same\n"))

        assert result.status is OperationStatus.SKIPPED
        assert backend.get_document(page_ref(HOME)).content == "# Same\n"

    # -- pages: update and versions -------------------------------------------

    def test_update_with_the_current_version_is_applied_and_changes_the_version(
        self, backend: DocumentProvider
    ) -> None:
        apply_one(backend, create_op(HOME, "# v1\n"))
        before = backend.get_document(page_ref(HOME))

        result = apply_one(backend, update_op(HOME, "# v2\n", expected=before.version))

        after = backend.get_document(page_ref(HOME))
        assert result.status is OperationStatus.APPLIED
        assert result.resolved_ref is not None and result.resolved_ref.locator["path"] == HOME
        assert after.content == "# v2\n"
        assert version_token(after.version) != version_token(before.version)
        assert version_token(result.resulting_version) == version_token(after.version)

    def test_update_without_an_expected_version_is_applied(self, backend: DocumentProvider) -> None:
        apply_one(backend, create_op(HOME, "# v1\n"))

        result = apply_one(backend, update_op(HOME, "# v2\n"))

        assert result.status is OperationStatus.APPLIED
        assert backend.get_document(page_ref(HOME)).content == "# v2\n"

    def test_update_with_identical_content_is_skipped(self, backend: DocumentProvider) -> None:
        apply_one(backend, create_op(HOME, "# same\n"))
        before = backend.get_document(page_ref(HOME))

        result = apply_one(backend, update_op(HOME, "# same\n", expected=before.version))

        assert result.status is OperationStatus.SKIPPED
        after = backend.get_document(page_ref(HOME))
        assert version_token(after.version) == version_token(before.version)

    def test_update_with_a_stale_version_fails_and_keeps_the_content(
        self, backend: DocumentProvider
    ) -> None:
        apply_one(backend, create_op(HOME, "# v1\n"))
        stale = backend.get_document(page_ref(HOME)).version
        apply_one(backend, update_op(HOME, "# v2\n"))

        result = apply_one(backend, update_op(HOME, "# v3\n", expected=stale))

        assert result.status is OperationStatus.FAILED
        assert result.message
        assert backend.get_document(page_ref(HOME)).content == "# v2\n"

    def test_update_of_a_missing_page_fails_without_creating_it(
        self, backend: DocumentProvider, root: Path
    ) -> None:
        result = apply_one(backend, update_op("Missing.md", "# x\n"))

        assert result.status is OperationStatus.FAILED
        assert not (root / "Missing.md").exists()

    def test_versions_are_content_hashes(self, backend: DocumentProvider) -> None:
        apply_all(backend, create_op("A.md", "# same\n"), create_op("B.md", "# same\n"))
        apply_one(backend, create_op("C.md", "# different\n"))

        token = {
            name: version_token(backend.get_document(page_ref(name)).version)
            for name in ("A.md", "B.md", "C.md")
        }

        assert token["A.md"] == token["B.md"]
        assert token["A.md"] != token["C.md"]

    # -- pages: child create --------------------------------------------------

    def test_child_create_with_an_explicit_ref_writes_that_root_page(
        self, backend: DocumentProvider, root: Path
    ) -> None:
        apply_one(backend, create_op(HOME, "# Home\n"))

        result = apply_one(backend, child_op(HOME, "Child.md", "# Child\n"))

        assert result.status is OperationStatus.APPLIED
        assert result.resolved_ref is not None
        assert result.resolved_ref.locator["path"] == "Child.md"
        assert (root / "Child.md").read_text(encoding="utf-8") == "# Child\n"

    # -- apply_changes never raises; failures are results ---------------------

    def test_a_failed_operation_does_not_stop_the_later_ones(
        self, backend: DocumentProvider, root: Path
    ) -> None:
        results = apply_all(
            backend,
            create_op("../escape.md", "# nope\n"),
            create_op("Good.md", "# ok\n"),
        )

        assert [r.status for r in results] == [OperationStatus.FAILED, OperationStatus.APPLIED]
        assert (root / "Good.md").read_text(encoding="utf-8") == "# ok\n"

    @pytest.mark.parametrize(
        "ref",
        [page_ref(None), page_ref("Home.md", kind=RefKind.ALIAS), page_ref("notes.txt")],
        ids=["no-path", "non-path-kind", "not-markdown"],
    )
    def test_unusable_refs_fail_the_operation_instead_of_raising(
        self, backend: DocumentProvider, root: Path, ref: DocumentRef
    ) -> None:
        before = snapshot(root)

        result = apply_one(
            backend, CreateDocumentOperation(ref=ref, title="T", content="# x\n")
        )

        assert result.status is OperationStatus.FAILED
        assert result.message
        assert snapshot(root) == before

    # -- confinement: no write outside the root, never under .git -------------

    @pytest.mark.parametrize("path", HOSTILE_PAGE_PATHS)
    def test_hostile_page_paths_fail_and_nothing_is_written_outside_the_root(
        self, backend: DocumentProvider, root: Path, tmp_path: Path, path: str
    ) -> None:
        before = snapshot(tmp_path)

        created = apply_one(backend, create_op(path, "# evil\n"))
        updated = apply_one(backend, update_op(path, "# evil\n"))

        assert created.status is OperationStatus.FAILED
        assert updated.status is OperationStatus.FAILED
        assert snapshot(tmp_path) == before
        assert not (root / ".git").exists()

    @pytest.mark.parametrize("path", HOSTILE_PAGE_PATHS)
    def test_hostile_page_paths_are_refused_when_reading(
        self, backend: DocumentProvider, path: str
    ) -> None:
        with pytest.raises(ConfigurationError):
            backend.exists(page_ref(path))
        with pytest.raises(ConfigurationError):
            backend.get_document(page_ref(path))

    # -- assets (GW-LB4) -------------------------------------------------------

    def test_put_asset_stores_the_bytes_under_the_root_and_returns_a_path_ref(
        self, backend: DocumentProvider, root: Path
    ) -> None:
        asset = backend.put_asset(asset_op("logo.png"), PNG)

        assert asset.ref.kind is AssetRefKind.PATH
        path = asset.ref.locator["path"]
        assert_canonical_relative(path)
        stored = root / path
        assert stored.read_bytes() == PNG
        assert stored.resolve().is_relative_to(root.resolve())

    def test_putting_the_same_asset_twice_is_idempotent(
        self, backend: DocumentProvider, root: Path
    ) -> None:
        first = backend.put_asset(asset_op("logo.png"), PNG)
        second = backend.put_asset(asset_op("logo.png"), PNG)

        assert first.ref.locator["path"] == second.ref.locator["path"]
        assert (root / first.ref.locator["path"]).read_bytes() == PNG

    def test_different_bytes_never_replace_an_earlier_asset(
        self, backend: DocumentProvider, root: Path
    ) -> None:
        first = backend.put_asset(asset_op("logo.png"), PNG)
        second = backend.put_asset(asset_op("logo.png"), OTHER_PNG)

        assert first.ref.locator["path"] != second.ref.locator["path"]
        assert (root / first.ref.locator["path"]).read_bytes() == PNG
        assert (root / second.ref.locator["path"]).read_bytes() == OTHER_PNG

    def test_the_assets_dir_option_selects_the_asset_directory(
        self, create_backend: Callable[..., DocumentProvider], root: Path
    ) -> None:
        backend = create_backend(assets_dir="media")

        asset = backend.put_asset(asset_op("logo.png"), PNG)

        path = asset.ref.locator["path"]
        assert path.startswith("media/")
        assert (root / path).read_bytes() == PNG

    @pytest.mark.parametrize("name", ["../../evil.png", "a/b.png", "..\\evil.png"])
    def test_a_hostile_asset_name_is_refused_or_kept_inside_the_root(
        self, backend: DocumentProvider, root: Path, tmp_path: Path, name: str
    ) -> None:
        before = snapshot(tmp_path)
        try:
            asset = backend.put_asset(asset_op(name), PNG)
        except ConfigurationError:
            assert snapshot(tmp_path) == before
            return

        stored = (root / asset.ref.locator["path"]).resolve()
        assert stored.is_relative_to(root.resolve())
        outside = {
            path: data
            for path, data in snapshot(tmp_path).items()
            if not path.startswith("wiki")
        }
        assert outside == {k: v for k, v in before.items() if not k.startswith("wiki")}

    def test_the_stored_asset_link_of_a_root_page_is_document_relative(
        self, backend: DocumentProvider, root: Path
    ) -> None:
        asset = backend.put_asset(asset_op("logo.png"), PNG)
        reference = backend.build_asset_reference(asset.ref)

        result = apply_one(backend, create_op(HOME, f"# Home\n\n![logo]({reference})\n"))

        assert result.status is OperationStatus.APPLIED
        match = _IMAGE_LINK.search((root / HOME).read_text(encoding="utf-8"))
        assert match is not None
        link = match.group(1)
        assert not link.startswith("/") and "://" not in link
        # github_wiki computes this reference itself, so it never depends on the
        # backend's own reference format: both must agree for a root page.
        assert link == build_asset_reference(asset.ref)
        assert (root / unquote(link)).read_bytes() == PNG

    def test_a_reference_the_backend_never_issued_is_left_untouched(
        self, backend: DocumentProvider, root: Path
    ) -> None:
        content = "# Home\n\n![logo](/assets/someone-elses.png)\n"

        apply_one(backend, create_op(HOME, content))

        assert (root / HOME).read_text(encoding="utf-8") == content

