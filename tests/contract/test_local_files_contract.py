"""The contract suite over the real ``local_files`` provider (GW-LB2, GW-LB5).

``local_files`` is used as-is: no code in it changed for ``github_wiki``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from wikiops_sdk.contracts import DocumentProvider
from wikiops_sdk.domain import OperationStatus

from tests.contract.local_backend_contract import (
    HOME,
    HOSTILE_PAGE_PATHS,
    LocalBackendContract,
    apply_one,
    create_op,
    page_ref,
    update_op,
)
from wikiops.core.exceptions import ConfigurationError
from wikiops.core.provider_manager import ProviderFactory
from wikiops.providers.local_files.provider import LocalFilesProviderFactory


class TestLocalFilesContract(LocalBackendContract):
    backend_type = "local_files"

    def make_factory(self) -> ProviderFactory:
        return LocalFilesProviderFactory()

    # -- GW-LB5: local_files' own error codes pass through unchanged ----------

    @pytest.mark.parametrize("path", HOSTILE_PAGE_PATHS)
    def test_apply_failures_keep_the_local_files_error_code(
        self, backend: DocumentProvider, path: str
    ) -> None:
        for operation in (create_op(path, "# x\n"), update_op(path, "# x\n")):
            result = apply_one(backend, operation)

            assert result.status is OperationStatus.FAILED
            assert result.message is not None
            assert result.message.startswith("[local_files:")

    @pytest.mark.parametrize(
        ("path", "code"),
        [
            ("../escape.md", "path.traversal"),
            ("/absolute.md", "path.absolute"),
            (".git/config.md", "path.reserved"),
        ],
    )
    def test_read_failures_keep_the_local_files_error_code(
        self, backend: DocumentProvider, path: str, code: str
    ) -> None:
        with pytest.raises(ConfigurationError) as exists_error:
            backend.exists(page_ref(path))
        with pytest.raises(ConfigurationError) as get_error:
            backend.get_document(page_ref(path))

        assert str(exists_error.value).startswith(f"[local_files:{code}]")
        assert str(get_error.value).startswith(f"[local_files:{code}]")

    def test_a_missing_document_keeps_the_local_files_error_code(
        self, backend: DocumentProvider, root: Path
    ) -> None:
        with pytest.raises(ConfigurationError) as caught:
            backend.get_document(page_ref(HOME))

        assert str(caught.value).startswith("[local_files:document.not_found]")
        assert not (root / HOME).exists()

    def test_the_conflict_message_of_a_blocked_create_is_the_local_files_one(
        self, backend: DocumentProvider
    ) -> None:
        apply_one(backend, create_op(HOME, "# Original\n"))

        result = apply_one(backend, create_op(HOME, "# Replacement\n"))

        assert result.message is not None
        assert result.message.startswith("[local_files:conflict.exists]")
