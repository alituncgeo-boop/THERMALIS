"""Dosya bütünlüğü: SHA-256 ve manifest doğrulaması (yalnız okuma)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

CHUNK = 4 * 1024 * 1024
# GDB'nin geçici kilit dosyaları içerik değildir; hiçbir zaman kopyalanmaz ve sayılmaz.
EPHEMERAL_SUFFIXES = (".lock",)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def is_ephemeral(path: Path) -> bool:
    return path.suffix.lower() in EPHEMERAL_SUFFIXES


@dataclass
class ManifestEntry:
    path: str  # kök klasöre göre, "/" ayırıcılı
    bytes: int
    sha256: str


@dataclass
class IntegrityResult:
    checked: int = 0
    problems: list[tuple[str, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict:
        return {
            "status": "PASS" if self.passed else "FAIL",
            "checked_files": self.checked,
            "problems": [{"path": p, "problem": k} for p, k in self.problems],
        }


def verify_tree(root: Path, entries: list[ManifestEntry], ignore: set[str] = frozenset()) -> IntegrityResult:
    """Her dosyanın varlığını, boyutunu ve SHA-256'sını denetler; beklenmeyen dosyaları raporlar."""
    result = IntegrityResult()
    expected = {e.path: e for e in entries}
    for entry in entries:
        file_path = root / Path(*entry.path.split("/"))
        result.checked += 1
        if not file_path.is_file():
            result.problems.append((entry.path, "MISSING"))
        elif file_path.stat().st_size != entry.bytes:
            result.problems.append((entry.path, "SIZE_MISMATCH"))
        elif sha256_file(file_path) != entry.sha256:
            result.problems.append((entry.path, "SHA256_MISMATCH"))
    for file_path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = file_path.relative_to(root).as_posix()
        if rel not in expected and rel not in ignore and not is_ephemeral(file_path):
            result.problems.append((rel, "UNEXPECTED_FILE"))
    return result
