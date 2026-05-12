import json

from service import Service


class FakeCLF:
    pass


class FakeRepository:
    pass


def test_load_uid_list_supports_multiple_json_shapes(tmp_path):
    list_file = tmp_path / "list.json"
    list_file.write_text(json.dumps(["AA:BB", "cc-dd", ""]))

    map_file = tmp_path / "map.json"
    map_file.write_text(json.dumps({"uids": ["11 22", "3344"]}))

    key_file = tmp_path / "key.json"
    key_file.write_text(json.dumps({"5566": {}, "77-88": {"name": "tag"}}))

    service = Service(FakeCLF(), FakeRepository())
    assert service._load_uid_list(str(list_file)) == {"AABB", "CCDD"}
    assert service._load_uid_list(str(map_file)) == {"1122", "3344"}
    assert service._load_uid_list(str(key_file)) == {"5566", "7788"}


def test_store_unknown_uid_is_normalized_and_deduplicated(tmp_path):
    unknown_file = tmp_path / "unknown.json"

    service = Service(
        FakeCLF(),
        FakeRepository(),
        new_nfc_uids_path=str(unknown_file),
    )
    service._store_unknown_nfc_uid("aa:bb")
    service._store_unknown_nfc_uid("AABB")
    service._store_unknown_nfc_uid("CC-DD")

    content = json.loads(unknown_file.read_text())
    assert content == {"uids": ["AABB", "CCDD"]}


def test_handle_non_homekey_tag_routes_known_and_unknown(tmp_path):
    known_file = tmp_path / "known.json"
    unknown_file = tmp_path / "unknown.json"
    known_file.write_text(json.dumps({"uids": ["ABCD"]}))

    service = Service(
        FakeCLF(),
        FakeRepository(),
        known_nfc_uids_path=str(known_file),
        new_nfc_uids_path=str(unknown_file),
        on_known_nfc_shell_command="known-cmd",
        on_unknown_nfc_shell_command="unknown-cmd",
    )

    called = []

    def fake_run(command, reason):
        called.append((command, reason))

    service._run_shell_command = fake_run
    service._handle_non_homekey_tag("ab:cd")
    service._handle_non_homekey_tag("1234")

    assert called == [
        ("known-cmd", "known-nfc"),
        ("unknown-cmd", "unknown-nfc"),
    ]
    assert json.loads(unknown_file.read_text()) == {"uids": ["1234"]}
