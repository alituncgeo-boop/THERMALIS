"""Çok parçalı yüklemenin kalıcı, sürdürülebilir durumu.

Durum dosyası işlem kimliğine (``operation_id``) göre yazılır ve gizli bilgi içermez (anahtar,
yükleme kimliği, parça ETag'leri, kaynak dosyanın boyutu/zamanı/SHA-256'sı). Konum: yapılandırma
klasörü (veri kökünün DIŞINDA). Süreç çökse bile aynı ``operation_id`` ile yükleme kaldığı yerden sürer.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..storage import write_json_atomic
from .errors import UploadStateInvalid

STATE_SCHEMA = "thermalis.multipart-upload/1"
_OP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_operation_id(operation_id: str) -> str:
    if not isinstance(operation_id, str) or not _OP_RE.match(operation_id):
        raise UploadStateInvalid("invalid_operation_id")
    return operation_id


@dataclass
class UploadState:
    operation_id: str
    bucket: str
    key: str
    source_path: str
    source_size: int
    source_mtime_ns: int
    sha256: str
    part_size: int
    upload_id: str | None = None
    parts: dict[str, dict] = field(default_factory=dict)  # "1" -> {"etag": ..., "size": ...}
    schema: str = STATE_SCHEMA

    @property
    def part_count(self) -> int:
        return max(1, -(-self.source_size // self.part_size))

    def part_bounds(self, number: int) -> tuple[int, int]:
        start = (number - 1) * self.part_size
        return start, min(self.part_size, self.source_size - start)


class UploadStateStore:
    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)

    def _path(self, operation_id: str) -> Path:
        return self.folder / f"{validate_operation_id(operation_id)}.json"

    def load(self, operation_id: str) -> UploadState | None:
        path = self._path(operation_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("schema") != STATE_SCHEMA:
                raise ValueError("schema")
            return UploadState(**data)
        except (ValueError, TypeError, AttributeError) as exc:
            raise UploadStateInvalid(f"unreadable_state:{operation_id}") from exc

    def save(self, state: UploadState) -> None:
        write_json_atomic(self._path(state.operation_id), asdict(state))

    def finish(self, operation_id: str) -> None:
        """Tamamlanan yüklemenin yerel durum dosyası kaldırılır (uzak nesneye dokunulmaz)."""
        self._path(operation_id).unlink(missing_ok=True)

    def pending(self) -> list[UploadState]:
        out = []
        for path in sorted(self.folder.glob("*.json")) if self.folder.is_dir() else []:
            try:
                state = self.load(path.stem)
            except UploadStateInvalid:
                continue
            if state is not None:
                out.append(state)
        return out
