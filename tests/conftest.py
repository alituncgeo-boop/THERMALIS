"""Test düzeneği: gerçek veriye dokunmadan HD01 yapısında sentetik aktarım paketi üretir."""

from __future__ import annotations

from pathlib import Path

import pytest

# Sentetik paket üreticisi pakete taşındı (canlı uçtan uca bulut testi de kullanır); testler aynı adlarla kullanır.
from thermalis.cloud.synthetic import (ESRI_WKT, ILCE_UAVT, YAKA, build_package, write_manifest,  # noqa: F401
                                       write_ready_marker)
from thermalis.cloud.synthetic import build_gdb as _build_gdb  # noqa: F401
from thermalis.cloud.synthetic import sha256_of as _sha  # noqa: F401


@pytest.fixture(autouse=True)
def allow_test_host(monkeypatch):
    """Uygulama yalnız yerel ad kabul eder; test istemcisinin ana bilgisayar adı 'testserver'dır."""
    monkeypatch.setenv("THERMALIS_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver")


@pytest.fixture()
def package(tmp_path: Path) -> Path:
    return build_package(tmp_path / "TT01_ADMIN_BOUNDARIES_v1.0.0-rc1")


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "THERMALIS_DATA"
    root.mkdir()
    return root
