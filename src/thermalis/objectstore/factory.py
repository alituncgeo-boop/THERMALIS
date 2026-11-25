"""Ayarlardan depo ve önbellek üretimi.

Uygulamanın kayıtlı sürüm deposu her zaman :func:`thermalis.data.catalog.release_store` (yerel) olarak
kalır. Bulut deposu yalnız açıkça istenen işlemler (durum, canlı duman testi; ileride Faz 04 kabulü)
için :func:`cloud_store` ile, belirli bir rolün kimlik bilgisiyle açılır.
"""

from __future__ import annotations

from pathlib import Path

from ..settings import Settings, config_dir, effective_data_root
from .cache import ObjectCache, default_cache_dir
from .credentials import install_log_redaction, load_credential
from .errors import CredentialsMissing
from .multipart import UploadStateStore
from .retry import RetryPolicy
from .s3 import MIB, S3Backend, make_client


def upload_state_dir() -> Path:
    return config_dir() / "upload-state"


def protected_roots(settings: Settings) -> list[Path]:
    roots = [effective_data_root(settings)]
    if settings.transfer_incoming_dir:
        roots.append(Path(settings.transfer_incoming_dir))
    return roots


def object_cache(settings: Settings) -> ObjectCache:
    root = Path(settings.cache.cache_dir) if settings.cache.cache_dir else default_cache_dir()
    return ObjectCache(root, settings.cache.max_bytes, protected_roots=protected_roots(settings))


def cloud_store(settings: Settings, role: str) -> S3Backend:
    cloud = settings.cloud
    if not cloud.endpoint or not cloud.bucket:
        raise CredentialsMissing("cloud_endpoint_or_bucket_not_configured")
    install_log_redaction()
    credential = load_credential(cloud.credential_profile, role)
    client = make_client(cloud.endpoint, credential, region=cloud.region, connect_timeout=cloud.connect_timeout_seconds,
                         read_timeout=cloud.read_timeout_seconds)
    return S3Backend(client, cloud.bucket, retry=RetryPolicy(max_attempts=cloud.max_attempts),
                     state_store=UploadStateStore(upload_state_dir()), part_size=cloud.multipart_part_size_mib * MIB,
                     multipart_threshold=cloud.multipart_part_size_mib * MIB)
