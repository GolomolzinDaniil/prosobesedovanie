"""Медиасервис (§3.5): изолированный декодер OGG/Opus, лимиты хранилища,
sweep и startup cleanup. Задача T15."""

from __future__ import annotations

import os
import struct
import time
from pathlib import Path

import pytest

from app.services.media_probe import parse_ogg_opus
from app.services.media_service import (
    MAX_MEDIA_FILES,
    MediaStorageFullError,
    check_capacity,
    create_private_file,
    directory_usage,
    ensure_media_dir,
    new_media_path,
    probe_ogg_opus,
    remove_file,
    run_probe,
    startup_cleanup,
    sweep_expired,
)
from tests.fakes import make_ogg_opus


def _write(path: Path, data: bytes, mode: int = 0o600) -> Path:
    path.write_bytes(data)
    os.chmod(path, mode)
    return path


class TestOggParser:
    def test_valid_opus_duration(self, tmp_path):
        p = _write(tmp_path / "a.ogg", make_ogg_opus(17.5))
        info = parse_ogg_opus(str(p))
        assert info["ok"] is True
        assert info["codec"] == "opus"
        assert info["duration_seconds"] == pytest.approx(17.5, abs=0.01)

    def test_not_ogg_container(self, tmp_path):
        p = _write(tmp_path / "b.ogg", b"RIFF....WAVEfmt " + b"\x00" * 64)
        info = parse_ogg_opus(str(p))
        assert info["ok"] is False
        assert info["error"] == "not_ogg_container"

    def test_ogg_but_not_opus(self, tmp_path):
        # валидная OGG-страница, но идентификационный заголовок не OpusHead
        payload = b"VorbisHead" + b"\x00" * 20
        header = b"OggS" + bytes([0, 0x02]) + struct.pack("<q", 0)
        header += struct.pack("<I", 1) + struct.pack("<I", 0) + struct.pack("<I", 0)
        header += bytes([1, len(payload)]) + payload
        p = _write(tmp_path / "c.ogg", header)
        info = parse_ogg_opus(str(p))
        assert info["ok"] is False
        assert info["error"] == "not_opus_codec"

    def test_broken_page_sync(self, tmp_path):
        data = bytearray(make_ogg_opus(5.0))
        # страница 0: 27 байт заголовка + 1 сегмент + 21 байт полезной нагрузки
        second_page = 27 + 1 + 8 + 11
        data[second_page:second_page + 4] = b"XXXX"
        p = _write(tmp_path / "d.ogg", bytes(data))
        info = parse_ogg_opus(str(p))
        assert info["ok"] is False
        assert info["error"] == "broken_page_sync"

    def test_empty_file(self, tmp_path):
        p = _write(tmp_path / "e.ogg", b"")
        info = parse_ogg_opus(str(p))
        assert info["ok"] is False
        assert info["error"] == "not_ogg_container"

    def test_missing_file(self, tmp_path):
        info = parse_ogg_opus(str(tmp_path / "nope.ogg"))
        assert info["ok"] is False
        assert info["error"].startswith("read:")


class TestIsolatedProbe:
    def test_run_probe_subprocess_ok(self, tmp_path):
        p = _write(tmp_path / "f.ogg", make_ogg_opus(120.0))
        info = run_probe(p, wall_timeout=10, memory_mib=256)
        assert info.ok is True
        assert info.duration_seconds == pytest.approx(120.0, abs=0.01)

    def test_run_probe_subprocess_rejects_bad(self, tmp_path):
        p = _write(tmp_path / "g.ogg", b"not audio at all")
        info = run_probe(p)
        assert info.ok is False
        assert info.error == "not_ogg_container"

    def test_probe_ogg_opus_helper(self, tmp_path):
        good = _write(tmp_path / "h.ogg", make_ogg_opus(3.0))
        bad = _write(tmp_path / "i.ogg", b"junkjunk")
        assert probe_ogg_opus(good) is not None
        assert probe_ogg_opus(bad) is None


class TestStorageRules:
    def test_dir_0700_file_0600_random_names(self, tmp_path):
        media = tmp_path / "media"
        ensure_media_dir(media)
        assert oct(media.stat().st_mode)[-3:] == "700"
        p1 = new_media_path(media)
        p2 = new_media_path(media)
        assert p1 != p2
        assert len(p1.stem) == 32  # случайное 32-символьное имя
        create_private_file(p1)
        assert oct(p1.stat().st_mode)[-3:] == "600"

    def test_capacity_total_limit(self, tmp_path):
        media = tmp_path / "media"
        ensure_media_dir(media)
        p = media / ("x" * 32 + ".ogg")
        _write(p, b"\x00" * (3 * 1024 * 1024))
        check_capacity(media, max_total_mib=8, incoming_bytes=1024)  # 3 MiB + 1 KiB ok
        with pytest.raises(MediaStorageFullError):
            check_capacity(media, max_total_mib=2, incoming_bytes=0)
        with pytest.raises(MediaStorageFullError):
            check_capacity(media, max_total_mib=8, incoming_bytes=6 * 1024 * 1024)

    def test_capacity_file_count_limit(self, tmp_path):
        media = tmp_path / "media"
        ensure_media_dir(media)
        for i in range(MAX_MEDIA_FILES):
            _write(media / f"{i:032d}.ogg", b"x")
        with pytest.raises(MediaStorageFullError):
            check_capacity(media, max_total_mib=256)

    def test_directory_usage(self, tmp_path):
        media = tmp_path / "media"
        ensure_media_dir(media)
        assert directory_usage(media) == (0, 0)
        _write(media / "a.ogg", b"12345")
        total, count = directory_usage(media)
        assert (total, count) == (5, 1)

    def test_remove_file_idempotent(self, tmp_path):
        p = _write(tmp_path / "j.ogg", b"data")
        remove_file(p)
        remove_file(p)  # повтор не падает
        remove_file(None)
        assert not p.exists()

    def test_sweep_expired_only_old(self, tmp_path):
        media = tmp_path / "media"
        ensure_media_dir(media)
        old = _write(media / "old.ogg", b"x")
        fresh = _write(media / "fresh.ogg", b"y")
        past = time.time() - 2 * 3600
        os.utime(old, (past, past))
        removed = sweep_expired(media, max_age_seconds=3600)
        assert removed == 1
        assert not old.exists() and fresh.exists()

    def test_startup_cleanup_removes_all(self, tmp_path):
        media = tmp_path / "media"
        ensure_media_dir(media)
        for name in ("a.ogg", "b.ogg"):
            _write(media / name, b"x")
        assert startup_cleanup(media) == 2
        assert directory_usage(media) == (0, 0)
        # отсутствующий каталог — не ошибка
        assert startup_cleanup(tmp_path / "missing") == 0
