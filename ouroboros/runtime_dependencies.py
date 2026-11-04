"""Interpreter eligibility and externally provisioned runtime readiness.

The runtime sync owns installation/custody and its success watermark. This module
only classifies the running interpreter and verifies the supported dependency
contract for an interpreter its package manager owns. A uv export is an install
recipe, not a compatibility maximum for distribution-managed packages.
"""
from __future__ import annotations

import importlib.metadata as metadata
import os
from pathlib import Path
import sys
import sysconfig
from typing import Mapping

INSTALL_RECIPES = ("requirements-runtime.lock", "requirements.txt")
DEPENDENCY_SOURCES = ("pyproject.toml", *INSTALL_RECIPES)


class DependencyReadinessError(RuntimeError):
    """No installation was attempted; the visible environment is not verified ready."""


def install_mode() -> str:
    """One eligibility decision for runtime sync and direct adoption planning.

    PIP_USER controls pip's target, not package ownership. Virtual environments
    take precedence over their base interpreter's marker (and embedded path).
    """
    if getattr(sys, "frozen", False):
        return "frozen:bundled"
    if os.environ.get("OUROBOROS_PYTEST_ACTIVE") == "1":
        return "pytest:suppressed"
    from ouroboros.platform_layer import interpreter_is_embedded

    if sys.prefix != sys.base_prefix or interpreter_is_embedded(sys.executable):
        return "install"
    marker = Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED"
    try:
        marker.stat()  # Permission/I/O errors are unknown, never permission to run pip.
    except FileNotFoundError:
        return "install"
    except OSError as exc:
        return f"unverified:marker:{type(exc).__name__}:{exc}"
    return "externally_managed"


def _toml(text: str) -> dict:
    try:
        try:
            import tomllib
        except ImportError:  # Python 3.10; pip is a declared runtime dependency.
            from pip._vendor import tomli as tomllib
        return tomllib.loads(text)
    except (ImportError, ValueError) as exc:
        raise DependencyReadinessError(f"TOML contract unavailable: {exc}") from exc


def requirement_sources(repo: Path) -> dict[str, str]:
    """Read only absence as absent; an explicit mapping may instead bind Git blobs."""
    result = {}
    for name in DEPENDENCY_SOURCES:
        try:
            result[name] = (repo / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            pass
    return result


def project_contract(sources: Mapping[str, str]) -> tuple[str, list[str], str]:
    """Supported direct dependencies/Python range, or the legacy requirement recipe.

    Optional groups aren't mandatory for headless source startup. Requested extras
    on mandatory dependencies are checked below using their installed metadata.
    """
    if "pyproject.toml" in sources:
        project = _toml(sources["pyproject.toml"]).get("project")
        if not isinstance(project, dict) or "dependencies" not in project:
            raise DependencyReadinessError("pyproject.toml has no static project.dependencies contract")
        dependencies = project["dependencies"]
        if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
            raise DependencyReadinessError("project.dependencies must be a list of requirement strings")
        python = project.get("requires-python", "")
        if not isinstance(python, str):
            raise DependencyReadinessError("project.requires-python must be a string")
        return "pyproject.toml:project.dependencies", dependencies, python
    # Old repositories have no supported-range declaration: honor their recipe,
    # including -r includes, rather than silently ignoring lines we cannot check.
    name = next((name for name in INSTALL_RECIPES if name in sources), "")
    if not name:
        return "fallback:minimal", ["openai>=1.0.0", "requests"], ""

    def lines(filename: str, seen: frozenset[str]) -> list[str]:
        if filename in seen or filename not in sources:
            raise DependencyReadinessError(f"unavailable or cyclic requirements include: {filename}")
        result = []
        for line in sources[filename].splitlines():
            text = line.split(" #", 1)[0].strip()
            if not text or text.startswith("#"):
                continue
            if text.startswith("-r "):
                result.extend(lines(text[3:].strip(), seen | {filename}))
            elif text.startswith("-") or text.endswith("\\"):
                raise DependencyReadinessError(f"unsupported external requirement directive: {text}")
            else:
                result.append(text)
        return result

    return name, lines(name, frozenset()), ""


def verify_external_requirements(repo: Path, *, sources: Mapping[str, str] | None = None) -> str:
    """Verify fresh visible metadata, including active markers and requested extras.

    This is version/metadata compatibility, not module-byte or import-origin proof.
    No pip, network, install, durable inventory or cached readiness is involved.
    """
    try:
        from packaging.requirements import Requirement
        from packaging.specifiers import SpecifierSet
        from packaging.utils import canonicalize_name

        source, requirements, python = project_contract(requirement_sources(repo) if sources is None else sources)
        if python and not SpecifierSet(python).contains(".".join(map(str, sys.version_info[:3])), prereleases=True):
            raise DependencyReadinessError(f"Python {sys.version.split()[0]} does not satisfy {python}")
        pending = [(text, frozenset({""})) for text in requirements]
        visited, distributions, failures = set(), {}, []
        while pending:
            text, marker_extras = pending.pop()
            request = Requirement(text)
            if request.marker and not any(request.marker.evaluate({"extra": extra}) for extra in marker_extras):
                continue
            if request.url:
                raise DependencyReadinessError(f"cannot verify a direct-URL requirement from version metadata: {text}")
            name = canonicalize_name(request.name)
            if name not in distributions:
                try:
                    distributions[name] = metadata.distribution(request.name)
                except metadata.PackageNotFoundError:
                    distributions[name] = None
            dist = distributions[name]
            if dist is None:
                failures.append(f"{text}: missing")
                continue
            if not request.specifier.contains(dist.version, prereleases=True):
                failures.append(f"{text}: installed {dist.version}")
            extras = frozenset({"", *request.extras})
            key = (name, extras)
            if key in visited:
                continue
            visited.add(key)
            required_python = dist.metadata.get("Requires-Python", "")
            if required_python and not SpecifierSet(required_python).contains(
                    ".".join(map(str, sys.version_info[:3])), prereleases=True):
                failures.append(f"{request.name}: Python does not satisfy {required_python}")
            provided = {canonicalize_name(value) for value in dist.metadata.get_all("Provides-Extra", [])}
            missing_extras = {canonicalize_name(value) for value in request.extras} - provided
            if missing_extras:
                failures.append(f"{request.name}: unavailable extras {', '.join(sorted(missing_extras))}")
            pending.extend((child, extras) for child in (dist.requires or []))
        if failures:
            raise DependencyReadinessError("; ".join(sorted(set(failures))))
        return f"externally_managed:verified:{source}"
    except DependencyReadinessError:
        raise
    except Exception as exc:
        raise DependencyReadinessError(f"dependency readiness unavailable: {type(exc).__name__}: {exc}") from exc
