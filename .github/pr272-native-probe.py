"""Native Windows smoke test of the exact Git _replace function, with no mocks.

Only the function AST is compiled, unchanged, to avoid the module's unrelated Unix-only
fcntl import. This verifies the native chmod/staging/replace path, not Windows support
for the complete service. Run with Python 3.12 on Windows and Git on PATH.
"""

import argparse
import ast
import contextlib
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import types
from pathlib import Path


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-c", f"safe.directory={repo.as_posix()}", "-C", str(repo), *args]
    )


def load_exact_function(repo, ref):
    commit = git(repo, "rev-parse", f"{ref}^{{commit}}").decode().strip()
    blob = git(repo, "rev-parse", f"{commit}:src/store.py").decode().strip()
    source_bytes = git(repo, "show", f"{commit}:src/store.py")
    source = source_bytes.decode("utf-8")
    parsed = ast.parse(source, filename=f"{commit}:src/store.py")
    functions = [
        node for node in parsed.body
        if isinstance(node, ast.FunctionDef) and node.name == "_replace"
    ]
    assert len(functions) == 1
    function = functions[0]
    assert not function.decorator_list
    segment = ast.get_source_segment(source, function)
    isolated = ast.Module(body=[function], type_ignores=[])
    namespace = {"os": os, "tempfile": tempfile, "contextlib": contextlib, "Path": Path}
    exec(compile(isolated, f"{commit}:src/store.py", "exec"), namespace)
    return namespace["_replace"], {
        "commit": commit,
        "storeGitBlob": blob,
        "storeSha256": hashlib.sha256(source_bytes).hexdigest(),
        "functionSha256": hashlib.sha256(segment.encode()).hexdigest(),
        "isolation": "exact unmodified _replace AST; standard-library native globals",
    }


def no_staging_files(root):
    assert not list(root.rglob("*.tmp")), "staging file leaked"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--ref", required=True)
    parser.add_argument("--baseline", help="Exact pre-fix revision expected to fail natively")
    args = parser.parse_args()
    repo = args.repo.resolve()

    assert os.name == "nt", "This test must run on native Windows"
    assert sys.version_info[:2] == (3, 12), "Use repository Python 3.12"
    assert not hasattr(os, "fchmod"), "Do not use a sitecustomize fchmod shim"
    assert os.chmod not in os.supports_fd, "This runtime must exercise the native path fallback"
    assert isinstance(os.chmod, types.BuiltinFunctionType), "os.chmod must not be patched"
    native_chmod = os.chmod
    native_replace = os.replace
    native_supports_fd = os.supports_fd.copy()
    events = []

    # Observe native calls without replacing os.chmod, os.replace, or capability sets.
    def observe(event, values):
        if event in {"os.chmod", "os.rename"}:
            events.append((event, values))

    sys.addaudithook(observe)
    report = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "osName": os.name,
        "fchmodPresent": hasattr(os, "fchmod"),
        "chmodSupportsFd": os.chmod in os.supports_fd,
        "chmodImplementation": f"{os.chmod.__module__}.{os.chmod.__name__}",
        "mocks": False,
        "scope": "native Windows exact _replace function; not full service portability",
        "permissionCaveat": "Windows chmod controls the read-only flag, not POSIX 0644 ACLs",
    }

    if args.baseline:
        baseline, details = load_exact_function(repo, args.baseline)
        with tempfile.TemporaryDirectory(prefix="pr272-before-") as folder:
            root = Path(folder)
            target = root / "new-count"
            try:
                baseline(target, b"7")
            except AttributeError as error:
                assert error.name == "fchmod", "Baseline failed for an unrelated reason"
                details["expectedFailure"] = f"{type(error).__name__}: {error}"
            else:
                raise AssertionError("Pre-fix native Windows baseline unexpectedly succeeded")
            assert not target.exists()
            no_staging_files(root)
            details["cleanedUp"] = True
        report["baseline"] = details

    replace, details = load_exact_function(repo, args.ref)
    results = []
    with tempfile.TemporaryDirectory(prefix="pr272-native-") as folder:
        root = Path(folder)
        target = root / "nested-\u017e" / "counter-\u03bb"

        for name, payload, fsync in [
            ("create_new_non_ascii_path", b"7", False),
            ("overwrite_existing_with_fsync", b"42\n", True),
            ("overwrite_with_empty_bytes", b"", False),
        ]:
            events.clear()
            replace(target, payload, fsync=fsync)
            assert target.read_bytes() == payload
            no_staging_files(root)
            chmods = [values for event, values in events if event == "os.chmod"]
            assert len(chmods) == 1, chmods
            staging_path, requested_mode, *_ = chmods[0]
            assert isinstance(staging_path, (str, bytes, os.PathLike)), chmods
            assert requested_mode == 0o644
            assert Path(staging_path).parent == target.parent
            assert Path(staging_path).name.startswith(f"{target.name}.")
            assert Path(staging_path).suffix == ".tmp"
            assert not Path(staging_path).exists()
            renames = [values for event, values in events if event == "os.rename"]
            assert len(renames) == 1
            assert Path(renames[0][0]) == Path(staging_path)
            assert Path(renames[0][1]) == target
            results.append({"case": name, "status": "PASS", "nativeChmodUsedStagingPath": True})

        # A real OS failure, not a patched os.replace: a file cannot replace this directory.
        blocked = root / "existing-directory"
        blocked.mkdir()
        sentinel = blocked / "preserved"
        sentinel.write_bytes(b"untouched")
        try:
            replace(blocked, b"must-not-replace-directory", fsync=True)
        except OSError as error:
            failure = f"{type(error).__name__}: winerror={getattr(error, 'winerror', None)}"
        else:
            raise AssertionError("Native replace of a nonempty directory unexpectedly succeeded")
        assert sentinel.read_bytes() == b"untouched"
        no_staging_files(root)
        results.append({"case": "native_replace_failure_cleanup", "status": "PASS", "error": failure})

    assert os.chmod is native_chmod
    assert os.replace is native_replace
    assert os.supports_fd == native_supports_fd
    assert not hasattr(os, "fchmod")
    details["cases"] = results
    report["target"] = details
    report["status"] = "PASS"
    print(json.dumps(report, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
