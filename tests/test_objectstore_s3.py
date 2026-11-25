"""S3/R2 arka ucu: çok parçalı yükleme, sürdürme, belirsiz yazım uzlaştırması, ağ hataları ve tel düzeyi başlıklar."""

from __future__ import annotations

import hashlib
import random
from pathlib import Path

import pytest

from s3fake import FakeS3, make_s3_backend
from thermalis.objectstore.base import CancelToken
from thermalis.objectstore.errors import (AlreadyExists, Cancelled, CapabilityNotSupported, InvalidKey, ObjectLocked,
                                          PreconditionFailed, StorageError, TransientError)
from thermalis.objectstore.multipart import UploadState, UploadStateStore
from thermalis.objectstore.retry import RetryPolicy, call_with_retry
from thermalis.objectstore.s3 import S3Backend, map_error, require_conditional_support, validate_endpoint

MIB = 1024 * 1024


def _file(tmp_path: Path, name: str, size: int, seed: int = 1) -> tuple[Path, str]:
    data = random.Random(seed).randbytes(size)
    path = tmp_path / name
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------------------------- çok parçalı

def test_multipart_upload_uses_conditional_completion(tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    path, sha = _file(tmp_path, "big.bin", 12 * MIB)
    info = store.upload_file("data/big.bin", path, expected_sha256=sha, operation_id="op-m1")
    assert server.calls["upload_part"] == 3 and server.calls["unconditional_complete"] == 0
    assert info.sha256 == sha and info.operation_id == "op-m1"
    assert store.verify_object("data/big.bin", sha, expected_size=12 * MIB).ok
    assert not list((tmp_path / "upload-state").glob("*.json"))  # tamamlanan yerel durum kaldırıldı


def test_multipart_completion_collision_is_rejected_and_own_upload_aborted(tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    a, sha_a = _file(tmp_path, "a.bin", 11 * MIB, seed=1)
    b, sha_b = _file(tmp_path, "b.bin", 11 * MIB, seed=2)
    store.upload_file("data/x.bin", a, expected_sha256=sha_a, operation_id="op-a")
    with pytest.raises(AlreadyExists):
        store.upload_file("data/x.bin", b, expected_sha256=sha_b, operation_id="op-b")
    assert store.verify_object("data/x.bin", sha_a).ok  # ilk nesnenin üzerine yazılmadı
    assert server.uploads == {}  # çakışan yüklemenin yarım parçaları bırakılmadı


def test_interrupted_multipart_resumes_after_restart(tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    path, sha = _file(tmp_path, "big.bin", 13 * MIB)
    original = server.upload_part

    def flaky_part(**kw):  # 1. parça geçer; 2. parça her denemede ağ hatası alır
        if kw["PartNumber"] == 2:
            server.fail_next("upload_part", "before")
        return original(**kw)

    server.upload_part = flaky_part
    with pytest.raises(TransientError):
        store.upload_file("data/big.bin", path, expected_sha256=sha, operation_id="op-r1")
    state = UploadStateStore(tmp_path / "upload-state").load("op-r1")
    assert state is not None and set(state.parts) == {"1"} and state.upload_id
    assert not store.exists("data/big.bin")  # tamamlanmamış yükleme görünmez

    server.upload_part = original
    before = server.calls["upload_part"]
    restarted = S3Backend(server, store.bucket, retry=store.retry, state_store=UploadStateStore(tmp_path / "upload-state"),
                          part_size=5 * MIB, multipart_threshold=5 * MIB)  # "yeniden başlatılmış" süreç
    info = restarted.upload_file("data/big.bin", path, expected_sha256=sha, operation_id="op-r1")
    assert server.calls["upload_part"] - before == 2  # yalnız eksik 2. ve 3. parça
    assert info.operation_id == "op-r1" and restarted.verify_object("data/big.bin", sha).ok


def test_resume_rejects_changed_source(tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    path, sha = _file(tmp_path, "big.bin", 11 * MIB)
    states = UploadStateStore(tmp_path / "upload-state")
    states.save(UploadState("op-s", store.bucket, "data/big.bin", str(path), path.stat().st_size,
                            path.stat().st_mtime_ns + 1, sha, 5 * MIB, upload_id="upl-x"))
    with pytest.raises(StorageError) as exc:
        store.upload_file("data/big.bin", path, expected_sha256=sha, operation_id="op-s")
    assert exc.value.code == "upload_state_invalid"


def test_ambiguous_multipart_completion_is_reconciled(tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    path, sha = _file(tmp_path, "big.bin", 11 * MIB)
    server.fail_next("complete_multipart_upload", "after")  # sunucu tamamladı, yanıt kayboldu
    info = store.upload_file("data/big.bin", path, expected_sha256=sha, operation_id="op-amb")
    assert info.operation_id == "op-amb" and store.verify_object("data/big.bin", sha).ok


# ----------------------------------------------------------------------------- belirsiz yazım / ağ

def test_ambiguous_put_is_reconciled_by_operation_id(tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    server.fail_next("put_object", "after")
    info = store.put_bytes("k/a.json", b"{}", create_only=True, operation_id="op-1")
    assert info.operation_id == "op-1" and server.calls["put_object"] == 2 and server.calls["unconditional_put"] == 0


def test_ambiguous_put_after_exhausted_retries_is_reconciled(tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    server.fail_next("put_object", "after")
    server.fail_next("put_object", "before", times=3)
    info = store.put_bytes("k/a.json", b"{}", create_only=True, operation_id="op-1")
    assert info.operation_id == "op-1"


def test_conditional_update_ambiguity_is_reconciled(tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    v1 = store.put_bytes("state/s.json", b"1", create_only=True, operation_id="op-1")
    server.fail_next("put_object", "after")
    v2 = store.put_bytes("state/s.json", b"2", if_match=v1.etag, operation_id="op-2")
    assert v2.operation_id == "op-2"
    with store.open_read("state/s.json") as h:
        assert h.read() == b"2"


def test_network_failures_are_bounded(tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    server.fail_next("put_object", "before", times=4)
    with pytest.raises(TransientError):
        store.put_bytes("k/b.json", b"{}", create_only=True, operation_id="op-1")
    assert server.calls["put_object"] == 4 and "k/b.json" not in server.objects


def test_backoff_is_exponential_with_jitter_and_cancellable() -> None:
    policy = RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=4.0)
    rng = random.Random(7)
    for attempt in range(6):
        assert 0.0 <= policy.delay(attempt, rng) <= min(4.0, 2 ** attempt)
    delays: list[float] = []
    zero = RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0)
    with pytest.raises(TransientError):
        call_with_retry(lambda _: (_ for _ in ()).throw(TransientError("x")), policy=zero, on_retry=lambda a, e, d: delays.append(d))
    assert len(delays) == 2
    token = CancelToken()
    with pytest.raises(Cancelled):
        call_with_retry(lambda _: (_ for _ in ()).throw(TransientError("x")), policy=RetryPolicy(max_attempts=5, base_delay=30),
                        cancel=token, on_retry=lambda a, e, d: token.cancel())  # 30 sn beklemeden iptal edilir
    with pytest.raises(PreconditionFailed):  # koşul hatası asla tekrar denenmez
        call_with_retry(lambda _: (_ for _ in ()).throw(PreconditionFailed("x")), policy=zero)


def test_streaming_read_resumes_mid_body_on_same_version(tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    data = random.Random(3).randbytes(3 * MIB)
    store.put_bytes("r/x.bin", data, create_only=True, operation_id="op")
    server.flaky_reads = [1000]
    with store.open_read("r/x.bin") as h:
        assert h.read() == data
    assert server.calls["get_object"] == 2  # kesintiden sonra Range + If-Match ile devam


def test_streaming_read_stops_if_object_changes_mid_read(tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    store.put_bytes("r/x.bin", b"a" * 5000, create_only=True, operation_id="op")
    server.flaky_reads = [1000]
    with store.open_read("r/x.bin") as h:
        h.read(10)
        server.objects["r/x.bin"]["etag"] = '"changed"'  # araya başka sürüm girdi
        with pytest.raises(PreconditionFailed):
            h.read()


def test_bucket_lock_conflict_is_reported(tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    v1 = store.put_bytes("locked/x", b"1", create_only=True, operation_id="op")
    server.objects["locked/x"]["locked"] = True
    with pytest.raises(ObjectLocked):
        store.put_bytes("locked/x", b"2", if_match=v1.etag, operation_id="op-2")


def test_pagination_token_missing_is_an_error(tmp_path: Path) -> None:
    class Broken(FakeS3):
        def list_objects_v2(self, **kw):
            return {"Contents": [], "IsTruncated": True}

    store = S3Backend(Broken(), "thermalis-test-bucket", retry=RetryPolicy(max_attempts=1, base_delay=0))
    with pytest.raises(StorageError):
        list(store.list("x/"))


# ----------------------------------------------------------------------------- yapılandırma

@pytest.mark.parametrize("endpoint", ["http://abc.r2.cloudflarestorage.com", "https://abc.r2.cloudflarestorage.com/bucket",
                                      "https://user:pw@abc.r2.cloudflarestorage.com", "https://abc?x=1", "", "abc"])
def test_endpoint_must_be_plain_https_host(endpoint: str) -> None:
    with pytest.raises(InvalidKey):
        validate_endpoint(endpoint)
    assert validate_endpoint("https://0123abcd.r2.cloudflarestorage.com/") == "https://0123abcd.r2.cloudflarestorage.com"


def test_invalid_bucket_and_part_size_are_rejected() -> None:
    with pytest.raises(InvalidKey):
        S3Backend(FakeS3(), "Bad_Bucket")
    with pytest.raises(ValueError):
        S3Backend(FakeS3(), "thermalis-test-bucket", part_size=MIB)


def test_sdk_without_conditional_parameters_is_refused() -> None:
    class Shape:
        members = {"Bucket": 1, "Key": 1}

    class Model:
        def operation_model(self, op):
            return type("Op", (), {"input_shape": Shape()})()

    client = type("C", (), {"meta": type("M", (), {"service_model": Model()})()})()
    with pytest.raises(CapabilityNotSupported):
        require_conditional_support(client)


# ----------------------------------------------------------------------------- tel düzeyi (gerçek botocore)

class _Raw:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def stream(self, **kw):
        yield self.body


def _wire_client(responses):
    """Gerçek boto3 istemcisi; ağ yerine ``before-send`` ile yanıt verir ve giden başlıkları kaydeder."""
    from botocore.awsrequest import AWSResponse
    from thermalis.objectstore.credentials import Credential, Secret
    from thermalis.objectstore.s3 import make_client

    sent: list[dict] = []
    cred = Credential("default", "publisher", "AKIDTESTFAKEKEY0001", Secret("fake-secret-value-never-real-0001"))
    client = make_client("https://0123abcd.r2.cloudflarestorage.com", cred)

    def handler(request, **kwargs):
        headers = {k.lower(): (v.decode("utf-8") if isinstance(v, bytes) else v) for k, v in request.headers.items()}
        sent.append({"method": request.method, "url": request.url, "headers": headers})
        status, headers, body = responses.pop(0)
        return AWSResponse(request.url, status, headers, _Raw(body))

    client.meta.events.register("before-send.s3", handler)
    return client, sent


def test_wire_put_create_only_and_if_match_headers() -> None:
    client, sent = _wire_client([(200, {"ETag": '"e1"'}, b""), (200, {"ETag": '"e2"'}, b"")])
    store = S3Backend(client, "thermalis-data-dev", retry=RetryPolicy(max_attempts=1, base_delay=0))
    store.put_bytes("k/a.json", b"{}", create_only=True, operation_id="op-1")
    store.put_bytes("k/a.json", b"{1}", if_match='"e1"', operation_id="op-2")
    first, second = sent
    assert first["method"] == "PUT" and first["headers"]["if-none-match"] == "*" and "if-match" not in first["headers"]
    assert second["headers"]["if-match"] == '"e1"' and "if-none-match" not in second["headers"]
    assert first["headers"]["x-amz-meta-thermalis-operation-id"] == "op-1"
    assert first["headers"]["x-amz-meta-thermalis-sha256"] == hashlib.sha256(b"{}").hexdigest()
    assert not any(h.startswith("x-amz-checksum") for h in first["headers"])  # varsayılan CRC başlığı yok
    assert "aws-chunked" not in first["headers"].get("content-encoding", "")
    assert "/thermalis-data-dev/k/a.json" in first["url"]  # path-style


def test_wire_complete_multipart_sends_if_none_match() -> None:
    complete_xml = b'<?xml version="1.0"?><CompleteMultipartUploadResult><ETag>"mp"</ETag></CompleteMultipartUploadResult>'
    sha = "a" * 64
    head = {"ETag": '"mp"', "Content-Length": "10", "x-amz-meta-thermalis-sha256": sha, "x-amz-meta-thermalis-operation-id": "op-c"}
    client, sent = _wire_client([(200, {}, complete_xml), (200, head, b"")])
    store = S3Backend(client, "thermalis-data-dev", retry=RetryPolicy(max_attempts=1, base_delay=0))
    state = UploadState("op-c", "thermalis-data-dev", "k/big.bin", "x", 10, 1, sha, 5 * MIB, upload_id="U1",
                        parts={"1": {"etag": '"p1"', "size": 10}})
    info = store._complete("k/big.bin", state, CancelToken())
    complete = sent[0]
    assert complete["method"] == "POST" and "uploadId=U1" in complete["url"]
    assert complete["headers"]["if-none-match"] == "*"
    assert info.sha256 == sha and info.operation_id == "op-c"


def test_wire_412_and_409_are_mapped_without_leaking_secrets() -> None:
    err412 = b'<?xml version="1.0"?><Error><Code>PreconditionFailed</Code><Message>At least one precondition failed</Message></Error>'
    err409 = (b'<?xml version="1.0"?><Error><Code>ObjectLockedByBucketPolicy</Code>'
              b'<Message>fake-secret-value-never-real-0001</Message></Error>')
    client, sent = _wire_client([(412, {}, err412), (404, {}, b""), (409, {}, err409)])
    store = S3Backend(client, "thermalis-data-dev", retry=RetryPolicy(max_attempts=1, base_delay=0))
    with pytest.raises(AlreadyExists):  # 412 → uzlaştırma (HEAD 404) → gerçek çakışma
        store.put_bytes("k/a.json", b"{}", create_only=True, operation_id="op-1")
    with pytest.raises(ObjectLocked) as exc:
        store.put_bytes("k/a.json", b"{}", if_match='"x"', operation_id="op-2")
    assert "fake-secret" not in str(exc.value) and "fake-secret" not in repr(exc.value.as_dict())


def test_error_mapping_table() -> None:
    from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError, ReadTimeoutError

    def ce(code, status):
        return ClientError({"Error": {"Code": code, "Message": "m"}, "ResponseMetadata": {"HTTPStatusCode": status}}, "Op")

    assert map_error(ce("PreconditionFailed", 412)).code == "precondition_failed"
    assert map_error(ce("ObjectLockedByBucketPolicy", 409)).code == "object_locked"
    assert map_error(ce("NoSuchKey", 404)).code == "not_found"
    assert map_error(ce("InvalidRange", 416)).code == "range_not_satisfiable"
    assert map_error(ce("AccessDenied", 403)).code == "access_denied"
    assert map_error(ce("SlowDown", 503)).retryable and map_error(ce("InternalError", 500)).retryable
    assert map_error(ce("Throttled", 429)).retryable
    assert not map_error(ce("PreconditionFailed", 412)).retryable
    assert map_error(EndpointConnectionError(endpoint_url="https://x")).retryable
    assert map_error(ReadTimeoutError(endpoint_url="https://x")).retryable
    assert map_error(NoCredentialsError()).code == "credentials_missing"
