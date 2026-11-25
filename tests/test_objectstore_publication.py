"""Yayın protokolü: commit işareti olmadan sürüm görünmez; kurtarma, çakışma ve doğrulama hatası (sentetik veri)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from s3fake import make_s3_backend
from thermalis.objectstore.errors import AlreadyExists, PublicationError, PublicationNotAuthorized, TransientError
from thermalis.objectstore.local import LocalBackend
from thermalis.objectstore.publication import (COMMIT_NAME, PublicationPolicy, ReleaseFile, committed_releases, inspect_release,
                                               pointer_key, publish_release, reconcile_dataset_pointer, release_prefix)

DS = "THERMALIS_DATA/SYN01_SYNTHETIC/TESTKENT"
ALLOW = PublicationPolicy(allow_cloud_publication=True)
RECORDS = {n: json.dumps({"synthetic": True, "name": n}).encode() for n in ("version.json", "quality.json", "integrity.json")}


def _files(tmp_path: Path, n: int = 3) -> list[ReleaseFile]:
    out = []
    for i in range(n):
        data = os.urandom(1000 + i)
        path = tmp_path / f"f{i}.bin"
        path.write_bytes(data)
        out.append(ReleaseFile(f"layer/f{i}.bin", path, hashlib.sha256(data).hexdigest(), len(data)))
    return out


@pytest.fixture(params=["local", "s3"])
def env(request, tmp_path: Path):
    if request.param == "local":
        return LocalBackend(tmp_path / "bucket"), None
    return make_s3_backend(tmp_path)


def _publish(store, files, op="op-1", version="1.0.0", **kw):
    return publish_release(store, dataset_id=DS, version=version, access_class=kw.pop("access_class", "SYNTHETIC"), files=files,
                           records=kw.pop("records", RECORDS), operation_id=op, policy=kw.pop("policy", ALLOW))


def test_publication_is_disabled_by_default_and_restricted_data_is_refused(env, tmp_path: Path) -> None:
    store, _ = env
    files = _files(tmp_path)
    with pytest.raises(PublicationNotAuthorized):
        _publish(store, files, policy=PublicationPolicy())
    for access in ("RESTRICTED", "UNKNOWN", "PUBLIC"):
        with pytest.raises(PublicationNotAuthorized):
            _publish(store, files, access_class=access)
    assert list(store.list()) == []  # hiçbir şey yazılmadı


def test_committed_release_is_visible_and_fully_verifiable(env, tmp_path: Path) -> None:
    store, _ = env
    commit = _publish(store, _files(tmp_path))
    prefix = release_prefix(DS, "1.0.0")
    assert prefix == "releases/SYN01_SYNTHETIC/TESTKENT/1.0.0/"
    assert commit["verification"] == "full_sha256_read_back" and len(commit["objects"]) == 6
    assert [(p, c["operation_id"]) for p, c, problem in committed_releases(store)] == [(prefix, "op-1")]
    assert inspect_release(store, prefix, full=True).status == "COMMITTED"
    assert _publish(store, _files(tmp_path), op="op-1")["operation_id"] == "op-1"  # tekrar: zaten tamam
    with pytest.raises(AlreadyExists):
        _publish(store, _files(tmp_path), op="op-2")  # aynı sürüme başka işlem


def test_incomplete_release_is_invisible_and_resumable(tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    files = _files(tmp_path)
    original = server.put_object
    armed = {"done": False}

    def crash_on_second(**kw):  # 2. dosyada tüm yeniden denemeler ağ hatası alır (bir kez kurulur)
        if kw["Key"].endswith("f1.bin") and not armed["done"]:
            armed["done"] = True
            server.fail_next("put_object", "before", times=4)
        return original(**kw)

    server.put_object = crash_on_second
    with pytest.raises(TransientError):
        _publish(store, files, op="op-crash")
    server.put_object = original
    prefix = release_prefix(DS, "1.0.0")
    assert list(committed_releases(store)) == []  # nesneler var ama işaret yok → görünmez
    state = inspect_release(store, prefix)
    assert (state.status, state.operation_ids) == ("INCOMPLETE", ("op-crash",))
    with pytest.raises(AlreadyExists):
        _publish(store, files, op="op-other")  # başka işlem yarım yayını devralamaz/üzerine yazamaz
    puts_before = server.calls["put_object"]
    _publish(store, files, op="op-crash")  # aynı işlem kimliğiyle sürdür
    assert inspect_release(store, prefix, full=True).status == "COMMITTED"
    assert server.calls["put_object"] - puts_before >= 1


def test_failed_verification_prevents_commit(tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    server.corrupt_on_write = {"f2.bin"}  # depolama katmanında sessiz bozulma taklidi
    with pytest.raises(PublicationError) as exc:
        _publish(store, _files(tmp_path))
    assert "verification_failed_release_not_committed" in str(exc.value) and "SHA256_MISMATCH" in str(exc.value)
    assert not store.exists(release_prefix(DS, "1.0.0") + COMMIT_NAME)
    assert list(committed_releases(store)) == []


def test_corrupt_or_tampered_marker_is_not_a_release(env, tmp_path: Path) -> None:
    store, _ = env
    prefix = release_prefix(DS, "2.0.0")
    store.put_bytes(prefix + "records/version.json", b"{}", create_only=True, operation_id="op-x")
    store.put_bytes(prefix + COMMIT_NAME, b'{"schema": "something-else"}', create_only=True, operation_id="op-x")
    ((p, commit, problem),) = list(committed_releases(store))
    assert commit is None and problem == "commit_schema_invalid"
    assert inspect_release(store, prefix).status == "CORRUPT"


def test_missing_required_records_are_rejected(env, tmp_path: Path) -> None:
    store, _ = env
    with pytest.raises(PublicationError):
        _publish(store, _files(tmp_path), records={"version.json": b"{}"})


def test_dataset_pointer_is_derived_from_markers_and_reconciled(env, tmp_path: Path) -> None:
    store, _ = env
    _publish(store, _files(tmp_path), version="1.0.0", op="op-a")
    _publish(store, _files(tmp_path), version="1.0.0-rc1", op="op-b")
    pointer = reconcile_dataset_pointer(store, DS, operation_id="op-p1")
    assert pointer["versions"] == ["1.0.0-rc1", "1.0.0"] and pointer["latest"] == "1.0.0" and pointer["authoritative"] is False
    _publish(store, _files(tmp_path), version="1.1.0", op="op-c")  # işaretçi güncellenmeden "kesildi"
    assert reconcile_dataset_pointer(store, DS, operation_id="op-p2")["latest"] == "1.1.0"
    assert reconcile_dataset_pointer(store, DS, operation_id="op-p3")["latest"] == "1.1.0"  # değişiklik yoksa yazmaz
    with store.open_read(pointer_key(DS)) as h:
        assert json.loads(h.read())["versions"] == ["1.0.0-rc1", "1.0.0", "1.1.0"]
