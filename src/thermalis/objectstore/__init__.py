"""THERMALIS nesne deposu soyutlaması (sağlayıcıdan bağımsız).

- :mod:`.base`         arayüz, tipler, doğrulama
- :mod:`.local`        yerel dosya sistemi arka ucu (varsayılan)
- :mod:`.s3`           S3 uyumlu arka uç (Cloudflare R2)
- :mod:`.cache`        SHA-256 adresli, sınırlı yerel önbellek
- :mod:`.credentials`  işletim sistemi kimlik deposu (Windows Credential Manager / keyring)
- :mod:`.publication`  değişmez sürüm yayın protokolü (commit işareti)

Uygulama, açık geçiş onayına kadar yalnız :class:`LocalBackend` kullanır.
"""

from .base import Capabilities, CancelToken, ObjectInfo, ObjectStore, VerificationResult
from .errors import StorageError
from .local import LocalBackend

__all__ = ["Capabilities", "CancelToken", "LocalBackend", "ObjectInfo", "ObjectStore", "StorageError", "VerificationResult"]
