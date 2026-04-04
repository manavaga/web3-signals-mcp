# tests/test_storage.py
import os
import tempfile
from storage.db import Storage


def _tmp_storage():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Storage(db_path=path), path


def test_save_and_load_agent_data():
    db, path = _tmp_storage()
    try:
        db.save("technical_agent", {"BTC": {"rsi": 42, "score": 65}})
        result = db.load_latest("technical_agent")
        assert result is not None
        assert result["BTC"]["rsi"] == 42
    finally:
        os.unlink(path)


def test_load_latest_returns_none_when_empty():
    db, path = _tmp_storage()
    try:
        assert db.load_latest("technical_agent") is None
    finally:
        os.unlink(path)


def test_save_and_load_kv():
    db, path = _tmp_storage()
    try:
        db.save_kv("test_ns", "key1", 3.14)
        assert db.load_kv("test_ns", "key1") == 3.14
        assert db.load_kv("test_ns", "missing") is None
    finally:
        os.unlink(path)


def test_save_and_load_kv_json():
    db, path = _tmp_storage()
    try:
        db.save_kv_json("test_ns", "key1", {"a": 1, "b": [2, 3]})
        result = db.load_kv_json("test_ns", "key1")
        assert result == {"a": 1, "b": [2, 3]}
    finally:
        os.unlink(path)


def test_performance_snapshot_lifecycle():
    db, path = _tmp_storage()
    try:
        sid = db.save_performance_snapshot("BTC", 63.2, "bullish", 84000.0, 3, "test")
        assert sid is not None
        unevaluated = db.load_unevaluated_snapshots(48, 0)
        assert len(unevaluated) >= 1
    finally:
        os.unlink(path)


def test_load_recent():
    db, path = _tmp_storage()
    try:
        db.save("tech", {"run": 1})
        db.save("tech", {"run": 2})
        results = db.load_recent("tech", days=1)
        assert len(results) == 2
    finally:
        os.unlink(path)


def test_load_history_with_pagination():
    db, path = _tmp_storage()
    try:
        for i in range(5):
            db.save("tech", {"run": i})
        page1 = db.load_history("tech", limit=2, offset=0)
        page2 = db.load_history("tech", limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
    finally:
        os.unlink(path)
