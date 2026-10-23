"""Local-only integration probes for PR #107 plus PR #614 (not submitted)."""

import os
from datetime import UTC, datetime

import orjson
import pytest
from _client import _keypair, _race_before_lock

import store


def stamp(epoch):
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def export_bytes(root, room):
    _, chunks = store.export_room(root, room)
    return b"".join(chunks)


def oracle(root, room, now):
    """Full scan deliberately does not rely on timestamp or sequence order."""
    cutoff = now - store.EPHEMERAL_TTL_SECONDS
    return b"".join(
        line + b"\n"
        for line in store.room_path(root, room).read_bytes().splitlines()
        if not store._expired(orjson.loads(line), cutoff)
    )


@pytest.mark.parametrize("signed", [False, True])
def test_prelock_interleaving_export_agrees_with_full_scan(tmp_path, monkeypatch, signed):
    now = 2_000_000_000.0
    expired = now - store.EPHEMERAL_TTL_SECONDS - 1
    stamps = iter((stamp(expired), stamp(now)))
    monkeypatch.setattr(store, "_now", lambda: next(stamps))
    monkeypatch.setattr(store.time, "time", lambda: now)
    marker = tmp_path / ".reaped"
    marker.touch()
    os.utime(marker, (now, now))
    room = "e-p-integration"
    path = store.room_path(tmp_path, room)

    def write(nick, text, nonce):
        if signed:
            did, sign = _keypair()
            return store.append(
                tmp_path, room, nick, text, did=did, nonce=nonce, sig=sign(f"{room}|{nonce}|{text}")
            )
        return store.append(tmp_path, room, nick, text)

    fired = _race_before_lock(
        monkeypatch,
        store,
        path,
        lambda: write("later", "committed-first", 1),
    )
    write("first", "committed-second", 2)
    assert fired
    records = [orjson.loads(line) for line in path.read_bytes().splitlines()]
    assert [record["text"] for record in records] == ["committed-first", "committed-second"]
    expected = oracle(tmp_path, room, now)
    assert expected
    assert export_bytes(tmp_path, room) == expected


@pytest.mark.parametrize("ages", [(61, 60, 60, 59), (61, 61), (60, 60), (59, 0)])
def test_export_matches_full_scan_at_equal_and_exact_ttl(tmp_path, monkeypatch, ages):
    now = 2_000_000_000.0
    monkeypatch.setattr(store, "EPHEMERAL_TTL_SECONDS", 60)
    monkeypatch.setattr(store.time, "time", lambda: now)
    room = "e-p-boundaries"
    path = store.room_path(tmp_path, room)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(
            orjson.dumps({"seq": i, "ts": stamp(now - age), "from": "bot", "text": f"line {i}"})
            + b"\n"
            for i, age in enumerate(ages, 1)
        )
    )
    assert export_bytes(tmp_path, room) == oracle(tmp_path, room, now)
