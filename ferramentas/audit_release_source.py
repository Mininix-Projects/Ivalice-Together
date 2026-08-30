"""Run a portable static audit of the publishable source tree."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import re


REPOSITORY = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPOSITORY / "source"
DEFAULT_REPORT = REPOSITORY / "artefatos" / "release_source_audit.json"

FORBIDDEN = re.compile(r"\.pytest_cache|__pycache__", re.IGNORECASE)
GENERATED_DIRECTORY_NAMES = {
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "runtime_selftest",
}
GENERATED_SUFFIXES = {".pyc", ".log", ".shutdown"}


def display_path(path: Path, repository: Path = REPOSITORY) -> str:
    """Return a stable repository-relative path when possible."""

    try:
        return path.resolve().relative_to(repository.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def function_rows(
    path: Path,
    tree: ast.AST,
    source_root: Path,
) -> list[dict[str, object]]:
    rows = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = int(getattr(node, "end_lineno", node.lineno))
            rows.append(
                {
                    "file": path.relative_to(source_root).as_posix(),
                    "name": node.name,
                    "line": node.lineno,
                    "lines": end - node.lineno + 1,
                    "arguments": len(node.args.args) + len(node.args.kwonlyargs),
                    "branches": sum(
                        isinstance(child, (ast.If, ast.For, ast.While, ast.Try, ast.Match))
                        for child in ast.walk(node)
                    ),
                }
            )
    return rows


def audit(source_root: Path) -> dict[str, object]:
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"source directory does not exist: {source_root}")

    python_files = sorted(source_root.glob("*.py"))
    functions: list[dict[str, object]] = []
    syntax_errors: list[dict[str, object]] = []
    forbidden_hits: list[dict[str, object]] = []
    imports: Counter[str] = Counter()
    modules = {path.stem for path in python_files}

    for path in python_files:
        source = path.read_text(encoding="utf-8-sig")
        for line_number, line in enumerate(source.splitlines(), 1):
            if FORBIDDEN.search(line):
                forbidden_hits.append(
                    {
                        "file": path.relative_to(source_root).as_posix(),
                        "line": line_number,
                        "text": line.strip()[:240],
                    }
                )
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as error:
            syntax_errors.append(
                {
                    "file": path.relative_to(source_root).as_posix(),
                    "error": str(error),
                }
            )
            continue
        functions.extend(function_rows(path, tree, source_root))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports[node.module.split(".", 1)[0]] += 1

    generated = []
    for path in source_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source_root)
        if (
            any(part in GENERATED_DIRECTORY_NAMES for part in relative.parts)
            or path.suffix.casefold() in GENERATED_SUFFIXES
        ):
            generated.append(
                {
                    "path": relative.as_posix(),
                    "bytes": path.stat().st_size,
                }
            )

    runtime_modules = sorted(
        module for module in modules if module not in imports and module != "coop_launcher"
    )
    return {
        "source_root": display_path(source_root),
        "python_files": len(python_files),
        "syntax_errors": syntax_errors,
        "forbidden_reference_hits": forbidden_hits,
        "generated_files": generated,
        "generated_bytes": sum(row["bytes"] for row in generated),
        "largest_functions": sorted(
            functions,
            key=lambda row: (row["lines"], row["branches"]),
            reverse=True,
        )[:60],
        "modules_without_direct_internal_import": runtime_modules,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=DEFAULT_SOURCE,
        help="source directory to audit (default: repository source/)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="JSON report path (default: artefatos/release_source_audit.json)",
    )
    parser.add_argument(
        "--strict-generated",
        action="store_true",
        help="fail when generated files are present below the source directory",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    report = audit(arguments.source_root)
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = {
        key: value
        for key, value in report.items()
        if key
        not in {
            "generated_files",
            "largest_functions",
            "modules_without_direct_internal_import",
        }
    }
    summary["generated_file_count"] = len(report["generated_files"])
    print(json.dumps(summary, indent=2))
    return int(
        bool(
            report["syntax_errors"]
            or report["forbidden_reference_hits"]
            or (arguments.strict_generated and report["generated_files"])
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
