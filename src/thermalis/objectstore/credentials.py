"""Bulut kimlik bilgileri: işletim sisteminin kimlik deposu (Windows Credential Manager, keyring ile).

Kurallar:
- Gizli değer repoda, ayar JSON'unda, komut satırı argümanında, günlükte, istisnada veya raporda bulunmaz.
- Değerler yalnız etkileşimli olarak (yankısız ``getpass``) girilir; sohbetten veya dosyadan alınmaz.
- Roller ayrıdır: ``uploader`` (ileride yalnız gelen paket yükleyen makine) ve ``publisher``
  (yayın yapan makine). Normal bir kova anahtarı önek kısıtlaması SAĞLAMAZ; gerçek ayrım için ayrı
  kovalar veya öneke kısıtlı geçici kimlik bilgileri gerekir (bkz. docs/cloud-storage.md).
- Güvensiz keyring arka uçları (düz metin dosya, "null"/"fail") reddedilir.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass

from .errors import CredentialsMissing, StorageError

SERVICE = "THERMALIS-ObjectStore"
ROLES = ("uploader", "publisher")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_INSECURE_BACKENDS = ("keyring.backends.fail", "keyring.backends.null", "keyrings.alt.file", "keyrings.alt.Gnome")
_KNOWN_SECRETS: set[str] = set()
_SECRETS_LOCK = threading.Lock()


class Secret:
    """Gizli değer sarmalayıcısı: ``repr``/``str``/biçimlendirme değeri asla göstermez."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise CredentialsMissing("empty_secret")
        self._value = value
        register_secret(value)

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "***"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:  # pragma: no cover - kullanılmaz
        return hash(("secret", len(self._value)))


@dataclass(frozen=True, repr=False)
class Credential:
    profile: str
    role: str
    access_key_id: str
    secret_access_key: Secret

    def __post_init__(self) -> None:
        register_secret(self.access_key_id)  # anahtar kimliği de günlüklerde gösterilmez

    def __repr__(self) -> str:
        return f"Credential(profile={self.profile!r}, role={self.role!r}, access_key_id=***, secret_access_key=***)"

    __str__ = __repr__


def register_secret(value: str) -> None:
    if value and len(value) >= 8:
        with _SECRETS_LOCK:
            _KNOWN_SECRETS.add(value)


def redact(text: str) -> str:
    """Metindeki bilinen gizli değerleri maskeler (günlük ve hata yolları için son savunma hattı)."""
    with _SECRETS_LOCK:
        secrets = sorted(_KNOWN_SECRETS, key=len, reverse=True)
    for value in secrets:
        if value in text:
            text = text.replace(value, "***")
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


_FACTORY_INSTALLED = False


def install_log_redaction() -> None:
    """Tüm günlük kayıtlarında bilinen gizli değerleri maskeler.

    Kayıt fabrikası düzeyinde kurulur: alt günlükçüler (``thermalis.x``, ``botocore.y``) dahil her kayıt,
    hangi işleyiciye giderse gitsin, oluşturulurken temizlenir. Bir kez kurulur.
    """
    global _FACTORY_INSTALLED
    with _SECRETS_LOCK:
        if _FACTORY_INSTALLED:
            return
        previous = logging.getLogRecordFactory()

        def factory(*args, **kwargs):
            record = previous(*args, **kwargs)
            RedactingFilter().filter(record)
            return record

        logging.setLogRecordFactory(factory)
        _FACTORY_INSTALLED = True


def _validate(profile: str, role: str) -> None:
    if not _NAME_RE.match(profile or ""):
        raise CredentialsMissing("invalid_profile_name")
    if role not in ROLES:
        raise CredentialsMissing("invalid_role")


def _keyring():
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover
        raise CredentialsMissing("keyring_not_installed") from exc
    backend = keyring.get_keyring()
    module = type(backend).__module__
    chained = getattr(backend, "backends", None)
    if any(module.startswith(m) for m in _INSECURE_BACKENDS) or (chained is not None and not chained):
        raise CredentialsMissing("no_secure_os_keyring_backend")
    return keyring


def _entry(profile: str, role: str) -> str:
    return f"{profile}:{role}"


def store_credential(profile: str, role: str, access_key_id: str, secret_access_key: str) -> None:
    _validate(profile, role)
    if not access_key_id or not secret_access_key or any(c.isspace() for c in access_key_id + secret_access_key):
        raise CredentialsMissing("empty_or_malformed_credential")
    register_secret(access_key_id)
    register_secret(secret_access_key)
    payload = json.dumps({"access_key_id": access_key_id, "secret_access_key": secret_access_key})
    try:
        _keyring().set_password(SERVICE, _entry(profile, role), payload)
    except CredentialsMissing:
        raise
    except Exception as exc:  # noqa: BLE001 - arka uç hatası gizli değer içermeden bildirilir
        raise StorageError(f"keyring_write_failed:{type(exc).__name__}") from None


def load_credential(profile: str, role: str) -> Credential:
    _validate(profile, role)
    try:
        raw = _keyring().get_password(SERVICE, _entry(profile, role))
    except CredentialsMissing:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StorageError(f"keyring_read_failed:{type(exc).__name__}") from None
    if not raw:
        raise CredentialsMissing(f"no_credential_for:{profile}:{role}")
    try:
        data = json.loads(raw)
        return Credential(profile, role, data["access_key_id"], Secret(data["secret_access_key"]))
    except (ValueError, KeyError, TypeError):
        raise CredentialsMissing(f"credential_unreadable:{profile}:{role}") from None


def delete_credential(profile: str, role: str) -> bool:
    _validate(profile, role)
    kr = _keyring()
    try:
        kr.delete_password(SERVICE, _entry(profile, role))
        return True
    except Exception:  # noqa: BLE001 - kayıt yok
        return False


def credential_status(profile: str) -> dict[str, bool]:
    """Her rol için kayıt var mı (değer döndürülmez)."""
    out = {}
    for role in ROLES:
        try:
            load_credential(profile, role)
            out[role] = True
        except (CredentialsMissing, StorageError):
            out[role] = False
    return out
