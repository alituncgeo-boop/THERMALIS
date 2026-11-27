# Manifest-listed source directories / Manifestli kaynak dizinleri

## English

The direct publisher recognizes a source directory with a root `MANIFEST.json` of type
`thermalis-directory-manifest-v1` and a `MANIFEST_SHA256.txt` anchor. Each
`files` row contains a portable relative `path`, byte length `bytes` and
`sha256` digest. The manifest and anchor travel with the release. Unlisted files,
aliases that collide across filesystems, symbolic links and Windows reparse points are refused.

Use the existing `thermalis storage declare-collection` and
`thermalis storage publish-collection` commands. The external declaration uses a
`directory` section with optional `documents` (manifest-relative document paths,
up to 2 MiB each). The usual quality source reference, citation and access class rules apply.
Files are hashed locally and streamed in place through the standard multipart and
full read-back commit pipeline. The local work directory holds indexing and release records.

The `SOURCE_FILES` catalog layer represents raw file preservation. Its access
descriptor reports `analysis_ready: false`. The normal materialization endpoint returns
the verified root manifest, or takes `item: "files"` and `asset` equal to one
manifest-relative file path to materialize that exact file from the commit. All published regular
file streams retain their bytes and nested paths. Directory names, including empty directories,
are recorded in `records/directory.json`. When a manifest declares an NTFS alternate
data stream inventory, the inventory travels as a regular file. Alternate stream bytes and
filesystem ACLs are outside this storage contract.

## Türkçe

Doğrudan yayımlama, kökünde `thermalis-directory-manifest-v1` türünde
`MANIFEST.json` ve onu doğrulayan `MANIFEST_SHA256.txt` bulunan kaynak dizinini
tanır. Dosya satırları göreli `path`, `bytes` ve `sha256` içerir.
Paket dışı beyanın `directory` bölümü katalogda gösterilecek küçük `documents`
dosyalarını seçebilir. Kaynak QA, atıf ve erişim koşulları korunur.

Dosyalar yerinde özetlenip mevcut çok parçalı yükleme, devam ve tam geri okuma
doğrulamasıyla ayrı nesneler olarak yayımlanır. `SOURCE_FILES` katmanı ham dosya
korumasını gösterir ve analiz hazırlığı `false` olarak bildirilir. Doğrulanmış manifest
veya `item: "files"` ile belirtilen tekil `asset` mevcut materialize uç noktasıyla
alınabilir. Normal dosya akışları ve iç içe yollar korunur. Boş dizinler de kayıt
altına alınır. Varsa NTFS alternatif akış envanteri dosya olarak saklanır.
Alternatif akış baytları ve dosya sistemi ACL’leri bu sözleşmenin kapsamı dışındadır.
