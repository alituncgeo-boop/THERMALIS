"""Kimlik bilgisi yönetimi ve gizli değerlerin hiçbir çıktıya sızmaması (canlı kimlik bilgisi kullanılmaz)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from thermalis.objectstore import credentials as cr
from thermalis.objectstore.errors import CredentialsMissing
from thermalis.objectstore.s3 import map_error
from thermalis.settings import Settings, load_settings, save_settings, settings_path

FAKE_ID = "TESTKEYID0123456789abcdef"
FAKE_SECRET = "test-secret-DO-NOT-USE-9f8e7d6c5b4a"


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def get_password(self, service, username):
        return self.items.get((service, username))

    def delete_password(self, service, username):
        if (service, username) not in self.items:
            raise PasswordDeleteError("missing")
        del self.items[(service, username)]


@pytest.fixture()
def memory_keyring():
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


def test_roundtrip_and_roles_are_separate(memory_keyring) -> None:
    cr.store_credential("default", "publisher", FAKE_ID, FAKE_SECRET)
    cred = cr.load_credential("default", "publisher")
    assert cred.access_key_id == FAKE_ID and cred.secret_access_key.reveal() == FAKE_SECRET
    assert cr.credential_status("default") == {"uploader": False, "publisher": True}
    with pytest.raises(CredentialsMissing):
        cr.load_credential("default", "uploader")
    assert cr.delete_credential("default", "publisher") and not cr.delete_credential("default", "publisher")
    with pytest.raises(CredentialsMissing):
        cr.load_credential("default", "admin")  # tanımsız rol
    with pytest.raises(CredentialsMissing):
        cr.store_credential("Default Profile", "publisher", FAKE_ID, FAKE_SECRET)


def test_secret_is_never_rendered(memory_keyring) -> None:
    cr.store_credential("default", "publisher", FAKE_ID, FAKE_SECRET)
    cred = cr.load_credential("default", "publisher")
    for rendered in (repr(cred), str(cred), f"{cred}", repr(cred.secret_access_key), f"{cred.secret_access_key}",
                     str([cred]), json.dumps({"c": str(cred)})):
        assert FAKE_SECRET not in rendered and FAKE_ID not in rendered


def test_insecure_keyring_backend_is_refused() -> None:
    from keyring.backends import fail

    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    try:
        with pytest.raises(CredentialsMissing) as exc:
            cr.store_credential("default", "publisher", FAKE_ID, FAKE_SECRET)
        assert "no_secure_os_keyring_backend" in str(exc.value)
    finally:
        keyring.set_keyring(previous)


def test_logs_and_errors_are_redacted(memory_keyring, caplog) -> None:
    cr.store_credential("default", "publisher", FAKE_ID, FAKE_SECRET)
    cr.load_credential("default", "publisher")
    cr.install_log_redaction()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("botocore.endpoint").debug("signing with %s and %s", FAKE_ID, FAKE_SECRET)
        logging.getLogger("thermalis.objectstore.x").warning(f"oops {FAKE_SECRET}")
    assert FAKE_SECRET not in caplog.text and FAKE_ID not in caplog.text and "***" in caplog.text
    from botocore.exceptions import ClientError

    leaked = ClientError({"Error": {"Code": "SignatureDoesNotMatch", "Message": f"key {FAKE_SECRET}"},
                          "ResponseMetadata": {"HTTPStatusCode": 403}}, "PutObject")
    mapped = map_error(leaked, "k/x")
    assert FAKE_SECRET not in str(mapped) and FAKE_SECRET not in json.dumps(mapped.as_dict()) and mapped.code == "access_denied"


def test_settings_never_contain_secrets_and_stay_backward_compatible(memory_keyring, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("THERMALIS_CONFIG_DIR", str(tmp_path / "config"))
    old = {"schema_version": "1.0", "language": "tr", "data_root": None, "active_project_id": None,
           "transfer_incoming_dir": None, "transfer_watch_enabled": True, "transfer_scan_interval_seconds": 30,
           "transfer_settle_seconds": 120}
    settings_path().parent.mkdir(parents=True)
    settings_path().write_text(json.dumps(old), encoding="utf-8")
    s = load_settings()  # yeni alanlar olmadan yazılmış eski ayar dosyası
    assert s.storage_backend == "local" and s.cloud.endpoint is None and s.cache.max_bytes == 20 * 1024 ** 3
    from thermalis.cli import main

    assert main(["storage", "configure-cloud", "--endpoint", "https://0123abcd.r2.cloudflarestorage.com",
                 "--bucket", "thermalis-data-dev"]) == 0
    cr.store_credential("default", "publisher", FAKE_ID, FAKE_SECRET)
    text = settings_path().read_text(encoding="utf-8")
    assert FAKE_SECRET not in text and FAKE_ID not in text
    assert json.loads(text)["storage_backend"] == "local"  # bulut ayarı depoyu etkinleştirmez
    Settings.model_validate({k: v for k, v in json.loads(text).items() if k in old})  # eski sürüm alanları geçerli


def test_cli_reads_secrets_only_through_hidden_prompt(memory_keyring, tmp_path: Path, monkeypatch, capsys) -> None:
    from thermalis.settings import CacheSettings

    monkeypatch.setenv("THERMALIS_CONFIG_DIR", str(tmp_path / "config"))
    save_settings(Settings(data_root=str(tmp_path / "data"), cache=CacheSettings(cache_dir=str(tmp_path / "cache"))))
    answers = iter([FAKE_ID, FAKE_SECRET])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    from thermalis.cli import main

    argv = ["storage", "credentials", "set", "--role", "uploader", "--allow-non-tty"]
    assert main(argv) == 0 and FAKE_SECRET not in " ".join(argv)
    assert main(["storage", "status"]) == 0
    out = capsys.readouterr()
    assert FAKE_SECRET not in out.out + out.err and FAKE_ID not in out.out + out.err
    assert "uploader=var" in out.out and "publisher=yok" in out.out
    assert cr.load_credential("default", "uploader").secret_access_key.reveal() == FAKE_SECRET


def test_live_smoke_test_requires_flag_and_environment(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("THERMALIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("THERMALIS_R2_LIVE_TEST", raising=False)
    from thermalis.cli import main

    assert main(["storage", "smoke-test", "--confirm-live"]) == 1
    monkeypatch.setenv("THERMALIS_R2_LIVE_TEST", "1")
    assert main(["storage", "smoke-test"]) == 1
    assert "live test disabled" in capsys.readouterr().err
