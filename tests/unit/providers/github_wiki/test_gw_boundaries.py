"""Architecture boundaries of the ``github_wiki`` package, checked on the AST (design D1).

* No module imports ``wikiops.providers.local_files``, in any form: ``import``,
  ``from ... import``, a relative import, an attribute chain on an imported
  package, or an ``importlib`` / ``__import__`` call. The wiki talks to its file
  backend only through the ``BackendResolver`` port, so the backend stays
  swappable.
* ``wikiops.core.provider_manager`` is imported only by ``backend.py`` and
  ``factory.py`` (the two modules that compose providers).
* The pure modules import no adapter module.

The scanner is itself tested on synthetic sources, so a green run cannot come
from a checker that sees nothing.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

import wikiops.providers.github_wiki as package_under_test

PACKAGE = "wikiops.providers.github_wiki"
PACKAGE_DIR = Path(package_under_test.__file__).resolve().parent

FORBIDDEN_BACKEND = "wikiops.providers.local_files"
PROVIDER_MANAGER = "wikiops.core.provider_manager"
PROVIDER_MANAGER_USERS = {"backend.py", "factory.py"}
PURE_MODULES = ("settings", "errors", "ports", "redaction", "classifier", "layout")
ADAPTER_MODULES = (
    "process",
    "auth",
    "git",
    "workdir",
    "manifest",
    "lock",
    "sync",
    "publisher",
    "backend",
    "provider",
    "factory",
)
DYNAMIC_IMPORTERS = {"import_module", "__import__", "find_spec", "import_string"}
DYNAMIC = "<dynamic>"


def _absolute(module: str | None, level: int, package: str) -> str:
    """Resolve a (possibly relative) ``from`` target to its absolute dotted name."""
    if level == 0:
        return module or ""
    parts = package.split(".")
    base = parts[: len(parts) - (level - 1)]
    return ".".join(base + (module.split(".") if module else []))


def _dotted(node: ast.AST) -> str | None:
    """``a.b.c`` for a chain of attribute accesses on a name, else ``None``."""
    names: list[str] = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(names)])
    return None


def referenced_modules(source: str, *, package: str = PACKAGE) -> set[str]:
    """Every dotted module name the source reaches: imports, attribute chains, dynamic imports."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(node.module, node.level, package)
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Attribute):
            chain = _dotted(node)
            if chain:
                names.add(chain)
        elif isinstance(node, ast.Call):
            callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if callee in DYNAMIC_IMPORTERS:
                first = node.args[0] if node.args else None
                literal = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else DYNAMIC
                names.add(literal)
    return names


def is_within(name: str, module: str) -> bool:
    return name == module or name.startswith(f"{module}.")


def reaches(source: str, module: str, *, package: str = PACKAGE) -> bool:
    return any(is_within(name, module) for name in referenced_modules(source, package=package))


def reaches_backend_or_is_dynamic(source: str) -> bool:
    names = referenced_modules(source)
    return DYNAMIC in names or any(is_within(name, FORBIDDEN_BACKEND) for name in names)


def package_modules() -> Iterator[tuple[str, Path]]:
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        yield path.relative_to(PACKAGE_DIR).as_posix(), path


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# -- the scanner itself -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "import wikiops.providers.local_files",
        "import wikiops.providers.local_files.provider as inner",
        "from wikiops.providers.local_files import LocalFilesProvider",
        "from wikiops.providers.local_files.provider import LocalFilesProviderFactory",
        "from wikiops.providers import local_files",
        "from wikiops.providers import _fs, local_files as files",
        "from .. import local_files",
        "from ..local_files import provider",
        "from ..local_files.provider import LocalFilesProvider",
        "import importlib\nimportlib.import_module('wikiops.providers.local_files')",
        "import importlib\nimportlib.import_module('wikiops.providers.local_files.provider')",
        "__import__('wikiops.providers.local_files')",
        "import importlib\nimportlib.import_module(some_name)",
        "import wikiops\nwikiops.providers.local_files.provider.LocalFilesProvider",
        "def late():\n    from wikiops.providers.local_files import provider\n    return provider",
    ],
)
def test_the_scanner_sees_every_way_of_reaching_the_local_files_backend(source: str) -> None:
    assert reaches_backend_or_is_dynamic(source)


@pytest.mark.parametrize(
    "source",
    [
        "from wikiops.providers import _fs",
        "from wikiops.providers._fs import validate_relative_path",
        "from wikiops.providers.github_wiki import layout",
        "from . import layout",
        "from .layout import validate_page_ref",
        "import wikiops.providers.local_files_extra",
        "from wikiops.providers.azure_devops import provider",
        "LOCAL = 'wikiops.providers.local_files'  # a string that is never imported",
        "\"\"\"Docs may mention wikiops.providers.local_files freely.\"\"\"",
        "import importlib\nimportlib.import_module('wikiops.core.exceptions')",
    ],
)
def test_the_scanner_ignores_everything_else(source: str) -> None:
    assert not reaches_backend_or_is_dynamic(source)


def test_the_scanner_resolves_relative_imports_against_the_package() -> None:
    assert _absolute("layout", 1, PACKAGE) == f"{PACKAGE}.layout"
    assert _absolute(None, 1, PACKAGE) == PACKAGE
    assert _absolute("local_files", 2, PACKAGE) == "wikiops.providers.local_files"
    assert _absolute("wikiops.core", 0, PACKAGE) == "wikiops.core"


def test_the_scanner_finds_the_provider_manager_in_each_import_form() -> None:
    for source in (
        "from wikiops.core.provider_manager import ProviderManager",
        "from wikiops.core import provider_manager",
        "import wikiops.core.provider_manager as manager",
    ):
        assert reaches(source, PROVIDER_MANAGER)
    assert not reaches("from wikiops.core.exceptions import ConfigurationError", PROVIDER_MANAGER)


# -- the package -------------------------------------------------------------------------------


def test_the_scan_covers_the_whole_package() -> None:
    names = {name for name, _ in package_modules()}

    assert {"provider.py", "backend.py", "sync.py", "layout.py", "settings.py"} <= names
    assert len(names) >= 15


def test_no_module_of_the_package_imports_the_local_files_backend() -> None:
    offenders = [
        name for name, path in package_modules() if reaches_backend_or_is_dynamic(read(path))
    ]

    assert offenders == []


def test_only_backend_and_factory_import_the_provider_manager() -> None:
    importers = {name for name, path in package_modules() if reaches(read(path), PROVIDER_MANAGER)}

    assert importers <= PROVIDER_MANAGER_USERS
    assert "backend.py" in importers  # the rule is not vacuous: backend.py really composes providers


@pytest.mark.parametrize("pure", PURE_MODULES)
def test_pure_modules_import_no_adapter_module(pure: str) -> None:
    path = PACKAGE_DIR / f"{pure}.py"
    assert path.is_file()
    source = read(path)

    adapters_reached = [
        adapter for adapter in ADAPTER_MODULES if reaches(source, f"{PACKAGE}.{adapter}")
    ]

    assert adapters_reached == []
    assert not reaches(source, PROVIDER_MANAGER)


@pytest.mark.parametrize(
    "source",
    [
        "from wikiops.providers.github_wiki import sync",
        "from wikiops.providers.github_wiki.git import Git",
        "from . import provider",
        "from .manifest import PendingManifest",
        "import wikiops.providers.github_wiki.auth",
    ],
)
def test_the_adapter_rule_catches_adapters_in_every_import_form(source: str) -> None:
    assert any(reaches(source, f"{PACKAGE}.{adapter}") for adapter in ADAPTER_MODULES)


def test_pure_and_adapter_module_lists_name_real_modules_and_do_not_overlap() -> None:
    existing = {path.stem for path in PACKAGE_DIR.glob("*.py")}

    assert set(PURE_MODULES) <= existing
    assert set(ADAPTER_MODULES) - {"publisher", "factory"} <= existing  # those two arrive in later slices
    assert not set(PURE_MODULES) & set(ADAPTER_MODULES)
