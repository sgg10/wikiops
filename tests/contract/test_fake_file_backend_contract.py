"""The contract suite over the test-only ``FakeFileBackend`` (GW-LB2).

Passing here proves the suite describes behavior that does not depend on
``local_files`` internals: a second, independent implementation satisfies it.
"""

from __future__ import annotations

from tests.contract.local_backend_contract import LocalBackendContract
from tests.support.fake_file_backend import FakeFileBackendFactory
from wikiops.core.provider_manager import ProviderFactory


class TestFakeFileBackendContract(LocalBackendContract):
    backend_type = "fake_files"

    def make_factory(self) -> ProviderFactory:
        return FakeFileBackendFactory()
