"""Every third-party import has to be declared, and declared in both places.

`xlrd` was imported by the national school connector, installed on the development
machine by accident, and listed in neither pyproject.toml nor the Dockerfile. It
therefore worked in every test and every local ingest, and failed only in the container -
which is the worst place to find out, and the hardest to notice, because the source just
reports an error and the other eleven carry on.
"""
import ast
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "api" / "talaia"

# Imported by the package but not third-party: our own code, and the standard library.
LOCAL = {"talaia"}
# Optional by design - the Rust core has a NumPy fallback, and the tests do not ship.
OPTIONAL = {"talaia_core", "pytest"}


def _declared_in_pyproject() -> set[str]:
    # tomllib, not a regex: "uvicorn[standard]>=0.32" carries a closing bracket, so
    # scanning to the first "]" stops after the second dependency and silently reports
    # everything else as undeclared.
    import tomllib

    with (ROOT / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    names = data.get("project", {}).get("dependencies", [])
    out = set()
    for spec in names:
        name = re.split(r"[<>=!\[; ]", spec, maxsplit=1)[0]
        if name:
            out.add(name.replace("-", "_").lower())
    return out


def _declared_in_dockerfile() -> set[str]:
    text = (ROOT / "Dockerfile").read_text()
    names = re.findall(r'"([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?(?:[<>=!]=?[^"]*)?"', text)
    return {n.split("[")[0].replace("-", "_").lower() for n in names}


def _imported_top_level() -> set[str]:
    found: set[str] = set()
    for path in PKG.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                # level > 0 is a relative import - our own package.
                if node.level == 0 and node.module:
                    found.add(node.module.split(".")[0])
    return found


def _third_party(names: set[str]) -> set[str]:
    stdlib = set(sys.stdlib_module_names)
    return {n for n in names if n not in stdlib and n not in LOCAL and n not in OPTIONAL}


# Distribution name != import name for a couple of these.
ALIASES = {"dotenv": "python_dotenv", "yaml": "pyyaml", "pydantic_settings": "pydantic_settings"}


@pytest.mark.parametrize("where,declared", [
    ("pyproject.toml", _declared_in_pyproject()),
    ("Dockerfile", _declared_in_dockerfile()),
])
def test_every_third_party_import_is_declared(where, declared):
    missing = set()
    for name in sorted(_third_party(_imported_top_level())):
        candidate = ALIASES.get(name, name).lower()
        if candidate not in declared and name.lower() not in declared:
            missing.add(name)
    assert not missing, (
        f"{where} does not declare {sorted(missing)}. An undeclared import works on a "
        f"machine that happens to have it and fails in the image.")


def test_the_two_dependency_lists_agree():
    """The Dockerfile installs a hand-copied list. Two lists drift."""
    only_pyproject = _declared_in_pyproject() - _declared_in_dockerfile()
    assert not only_pyproject, (
        f"declared in pyproject.toml but not installed in the image: "
        f"{sorted(only_pyproject)}")
