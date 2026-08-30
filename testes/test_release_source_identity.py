from __future__ import annotations

import importlib.util
import marshal
from pathlib import Path
from unittest import mock

import pytest


BUILDER_PATH = (
    Path(__file__).resolve().parents[1]
    / "ferramentas"
    / "build_public_release.py"
)
SPEC = importlib.util.spec_from_file_location(
    "build_public_release_identity_test",
    BUILDER_PATH,
)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class FakeArchive:
    def __init__(self, entries: dict[str, bytes]) -> None:
        self.entries = dict(entries)
        self.toc = set(self.entries)

    def extract(self, name: str):
        return self.entries[name]


class FakePyz:
    def __init__(self, entries: dict[str, object]) -> None:
        self.entries = dict(entries)
        self.toc = set(self.entries)

    def extract(self, name: str):
        return self.entries[name]


def compiled(source: str, filename: str):
    return compile(source, filename, "exec", dont_inherit=True, optimize=1)


def test_frozen_identity_audits_unlisted_local_modules(tmp_path: Path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "required.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source_root / "unlisted.py").write_text("VALUE = 2\n", encoding="utf-8")
    (source_root / "entry.py").write_text("VALUE = 3\n", encoding="utf-8")

    pyz = FakePyz(
        {
            "required": compiled("VALUE = 1\n", "required.py"),
            # This helper is deliberately outside the explicit critical list.
            "unlisted": compiled("VALUE = 999\n", "unlisted.py"),
        }
    )
    archive = FakeArchive(
        {
            "PYZ.pyz": b"fake-pyz",
            "entry": marshal.dumps(compiled("VALUE = 3\n", "entry.py")),
        }
    )

    with (
        mock.patch.object(builder, "ROOT", source_root),
        mock.patch.object(builder, "EMBEDDED_SOURCE_CONTRACT", ("required",)),
        mock.patch.object(builder, "EMBEDDED_ENTRY_SOURCE_CONTRACT", ("entry",)),
        mock.patch(
            "PyInstaller.archive.readers.CArchiveReader",
            return_value=archive,
        ),
        mock.patch(
            "PyInstaller.archive.readers.ZlibArchiveReader",
            return_value=pyz,
        ),
    ):
        with pytest.raises(RuntimeError, match="unlisted: frozen bytecode is stale"):
            builder.verify_embedded_sources(tmp_path / "runtime.exe")


def test_frozen_identity_rejects_pruned_unlisted_local_modules(tmp_path: Path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "required.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source_root / "pruned.py").write_text("VALUE = 2\n", encoding="utf-8")
    (source_root / "entry.py").write_text("VALUE = 3\n", encoding="utf-8")

    pyz = FakePyz(
        {
            "required": compiled("VALUE = 1\n", "required.py"),
        }
    )
    archive = FakeArchive(
        {
            "PYZ.pyz": b"fake-pyz",
            "entry": marshal.dumps(compiled("VALUE = 3\n", "entry.py")),
        }
    )

    with (
        mock.patch.object(builder, "ROOT", source_root),
        mock.patch.object(builder, "EMBEDDED_SOURCE_CONTRACT", ("required",)),
        mock.patch.object(builder, "EMBEDDED_ENTRY_SOURCE_CONTRACT", ("entry",)),
        mock.patch(
            "PyInstaller.archive.readers.CArchiveReader",
            return_value=archive,
        ),
        mock.patch(
            "PyInstaller.archive.readers.ZlibArchiveReader",
            return_value=pyz,
        ),
    ):
        with pytest.raises(RuntimeError, match="pruned: missing from runtime"):
            builder.verify_embedded_sources(tmp_path / "runtime.exe")


def test_frozen_identity_accepts_all_matching_local_modules(tmp_path: Path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    sources = {
        "required": "VALUE = 1\n",
        "unlisted": "VALUE = 2\n",
        "entry": "VALUE = 3\n",
    }
    for name, source in sources.items():
        (source_root / f"{name}.py").write_text(source, encoding="utf-8")

    pyz = FakePyz(
        {
            name: compiled(sources[name], f"{name}.py")
            for name in ("required", "unlisted")
        }
    )
    archive = FakeArchive(
        {
            "PYZ.pyz": b"fake-pyz",
            "entry": marshal.dumps(compiled(sources["entry"], "entry.py")),
        }
    )

    with (
        mock.patch.object(builder, "ROOT", source_root),
        mock.patch.object(builder, "EMBEDDED_SOURCE_CONTRACT", ("required",)),
        mock.patch.object(builder, "EMBEDDED_ENTRY_SOURCE_CONTRACT", ("entry",)),
        mock.patch(
            "PyInstaller.archive.readers.CArchiveReader",
            return_value=archive,
        ),
        mock.patch(
            "PyInstaller.archive.readers.ZlibArchiveReader",
            return_value=pyz,
        ),
    ):
        builder.verify_embedded_sources(tmp_path / "runtime.exe")
