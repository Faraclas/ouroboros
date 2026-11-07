"""Reproducible staged backend checks; all application writes stay disposable."""
from datetime import datetime, timezone
import argparse
import ast
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
import unittest

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-source", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    evidence = args.evidence_dir.resolve()
    if evidence == ROOT or ROOT in evidence.parents:
        parser.error("Evidence must be outside the reviewed payload")
    evidence.mkdir(parents=True, exist_ok=True)
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    host_root = args.host_source.resolve()
    sys.path.insert(0, str(host_root))
    import ouroboros
    assert Path(ouroboros.__file__).resolve().is_relative_to(host_root), "host import did not select the explicit source"
    modules = ("test_boards", "test_host_boundary")
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    source_count = sum(sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
                           for node in ast.walk(ast.parse((ROOT / (module + ".py")).read_text(encoding="utf-8"))))
                       for module in modules)
    assert suite.countTestCases() == source_count and source_count > 0
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    print(output.getvalue())
    # Additional repeated FIRST-open concurrency falsifier, not counted as new
    # unique test cases or a proof that every scheduling interleaving is safe.
    stress = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(
        "test_boards.StoreTests.test_real_crossprocess_first_init_duplicate_exactly_once") for _ in range(10))
    extra = unittest.TextTestRunner(stream=output, verbosity=1).run(stress) if result.wasSuccessful() else None
    if extra is not None:
        print(f"Fresh-init stress repetitions: {extra.testsRun}; success={extra.wasSuccessful()}")
    identities = {}
    for name in ("SKILL.md", "plugin.py", "board_store.py", "test_boards.py", "test_host_boundary.py", "BACKEND.md", "verify_backend.py"):
        data = (ROOT / name).read_bytes()
        identities[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "lines": len(data.splitlines())}
    report = {"schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
              "python": platform.python_version(), "interpreter": sys.executable,
              "sqlite": sqlite3.sqlite_version, "starlette": importlib.metadata.version("starlette"),
              "host_source": str(Path(ouroboros.__file__).resolve()),
              "unique_test_cases": result.testsRun, "source_enumerated_test_cases": source_count,
              "errors": len(result.errors), "failures": len(result.failures),
              "first_init_repetitions": extra.testsRun if extra else 0,
              "success": result.wasSuccessful() and extra is not None and extra.wasSuccessful(),
              "installed": False, "source_files": identities,
              "limits": ["staged ASGI and actual host ABI, not live installed extension dispatch",
                         "process-spawn leaf mocked for host canonical-root selection",
                         "isolated acting constraint-loss conservatively refuses; in-process explicit acting succeeds",
                         "no runtime lifecycle, provider, browser or owner-board mutation"]}
    (evidence / "verification-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (evidence / "verification-output.txt").write_text(output.getvalue(), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
