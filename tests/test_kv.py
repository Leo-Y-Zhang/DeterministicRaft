"""Unit tests for the KV state machine and its structured commands (harmonia/kv.py)."""

from harmonia.kv import CAS, GET, NOOP, PUT, Command, HistoryEntry, KVStateMachine


class TestCommandCodec:
    def test_put_round_trips(self):
        cmd = Command(2, 5, PUT, "k1", "v9")
        assert Command.decode(cmd.encode()) == cmd

    def test_get_round_trips(self):
        cmd = Command(0, 0, GET, "k2")
        assert Command.decode(cmd.encode()) == cmd

    def test_cas_round_trips(self):
        cmd = Command(1, 3, CAS, "k0", "v4", "v3")
        assert Command.decode(cmd.encode()) == cmd

    def test_opaque_string_decodes_to_noop(self):
        # the raft unit tests use bare strings like "a"/"orphan-0" as commands
        for s in ["a", "orphan-0", "survives", ""]:
            decoded = Command.decode(s)
            assert decoded.op == NOOP
            assert not decoded.is_structured

    def test_malformed_fields_decode_to_noop(self):
        assert Command.decode("x:y:put:k:v:").op == NOOP      # non-int ids
        assert Command.decode("0:0:frobnicate:k:v:").op == NOOP  # unknown op

    def test_is_structured(self):
        assert Command(0, 0, PUT, "k", "v").is_structured
        assert not Command(-1, -1, NOOP).is_structured


class TestKVStateMachine:
    def test_put_then_get(self):
        kv = KVStateMachine()
        assert kv.apply(Command(0, 0, PUT, "k", "v1")) == "ok"
        assert kv.apply(Command(0, 1, GET, "k")) == "v1"

    def test_get_absent_key_is_empty(self):
        assert KVStateMachine().apply(Command(0, 0, GET, "missing")) == ""

    def test_last_put_wins(self):
        kv = KVStateMachine()
        kv.apply(Command(0, 0, PUT, "k", "v1"))
        kv.apply(Command(0, 1, PUT, "k", "v2"))
        assert kv.apply(Command(0, 2, GET, "k")) == "v2"

    def test_cas_succeeds_when_expected_matches(self):
        kv = KVStateMachine()
        kv.apply(Command(0, 0, PUT, "k", "v1"))
        assert kv.apply(Command(0, 1, CAS, "k", "v2", "v1")) == "ok"
        assert kv.apply(Command(0, 2, GET, "k")) == "v2"

    def test_cas_fails_and_does_not_write_when_expected_mismatches(self):
        kv = KVStateMachine()
        kv.apply(Command(0, 0, PUT, "k", "v1"))
        assert kv.apply(Command(0, 1, CAS, "k", "v9", "vX")) == "fail"
        assert kv.apply(Command(0, 2, GET, "k")) == "v1"

    def test_cas_on_absent_key_matches_empty_expected(self):
        kv = KVStateMachine()
        assert kv.apply(Command(0, 0, CAS, "k", "v1", "")) == "ok"
        assert kv.store["k"] == "v1"

    def test_noop_has_no_effect(self):
        kv = KVStateMachine()
        kv.apply(Command(0, 0, PUT, "k", "v1"))
        assert kv.apply(Command.decode("opaque")) == ""
        assert kv.snapshot() == {"k": "v1"}

    def test_snapshot_is_sorted_copy(self):
        kv = KVStateMachine()
        for i in (2, 0, 1):
            kv.apply(Command(0, i, PUT, f"k{i}", f"v{i}"))
        snap = kv.snapshot()
        assert list(snap) == ["k0", "k1", "k2"]
        snap["k0"] = "mutated"
        assert kv.store["k0"] == "v0"  # snapshot is a copy


class TestHistoryEntry:
    def test_completed_flag(self):
        e = HistoryEntry(0, 0, GET, "k", "", "", invoke_step=3)
        assert not e.completed
        e.return_step = 7
        assert e.completed
