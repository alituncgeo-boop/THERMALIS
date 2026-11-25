"""THERMALIS DATA bulut iş akışı: paket yükleme (ikinci bilgisayar), bulut kabulü ve merkezi katalog.

- :mod:`.layout`         R2'deki anahtar düzeni (incoming / releases / catalog / state)
- :mod:`.authorization`  kısıtlı veri için bulut depolama yetki kaydı
- :mod:`.upload`         bağımsız paket yükleyici (yalnız ``upload`` pixi ortamı yeterlidir)
- :mod:`.intake`         buluttaki hazır paketlerin kabulü (mevcut QA ve kayıt mantığıyla)
- :mod:`.catalog_sync`   işaretli bulut sürümlerinin yerel kataloğa eşitlenmesi
- :mod:`.synthetic`      testler için sentetik paket üreticisi
"""
