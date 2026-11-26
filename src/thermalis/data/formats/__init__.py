"""Genel bilimsel veri biçimleri (biçime özgü yorum ayrı modüllerdedir).

- :mod:`.tar_index`   sıkıştırılmamış TAR arşivleri: tek geçişte üye konumları + üye ve arşiv SHA-256'sı
- :mod:`.raster`      GDAL ile okunabilen raster/COG başlık bilgisi (yerel yol, ``/vsisubfile/``, ``/vsis3/``)
- :mod:`.preflight`   dosya listesinden biçim ön denetimi (indirme/kopya yapmadan)
- :mod:`.landsat`     Landsat Collection 2 Level-2 metadata YORUMLAYICISI (genel bağdaştırıcıdan ayrı)

Hiçbir modül kaynak dosyayı değiştirmez, çıkarmaz veya kopyalamaz; değerler kaynakta beyan edildiği gibi
kaydedilir, dönüştürülmez (ör. sıcaklık ölçeği uygulanmaz).
"""
