import json

from service import Service


class FakeCLF:
    pass


class FakeRepository:
    pass


class FakeEndpoint:
    def __init__(self, endpoint_id, public_key_hex):
        self.id = bytes.fromhex(endpoint_id)
        self.public_key = bytes.fromhex(public_key_hex)


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


def test_known_uid_name_is_loaded_from_supported_shapes(tmp_path):
    known_file = tmp_path / "known_named.json"
    known_file.write_text(
        json.dumps(
            {
                "uids": [{"uid": "AA-BB", "name": "Alice card"}],
                "CCDD": "Guest card",
                "EEFF": {"name": "Spare card"},
            }
        )
    )

    service = Service(FakeCLF(), FakeRepository(), known_nfc_uids_path=str(known_file))

    assert service._get_known_nfc_uid_name("AABB") == (True, "Alice card")
    assert service._get_known_nfc_uid_name("CC:DD") == (True, "Guest card")
    assert service._get_known_nfc_uid_name("EE-FF") == (True, "Spare card")
    assert service._get_known_nfc_uid_name("FFFF") == (False, None)


def test_access_log_appends_json_lines_with_name(tmp_path):
    access_log = tmp_path / "access.log.jsonl"
    known_file = tmp_path / "known.json"
    known_file.write_text(json.dumps({"ABCD": "Alice tag"}))

    service = Service(
        FakeCLF(),
        FakeRepository(),
        known_nfc_uids_path=str(known_file),
        access_log_path=str(access_log),
    )
    service._run_shell_command = lambda *_: None
    service._handle_non_homekey_tag("ab:cd")

    log_entries = [json.loads(line) for line in access_log.read_text().splitlines()]
    assert len(log_entries) == 1
    assert log_entries[0]["event_type"] == "nfc_known"
    assert log_entries[0]["uid"] == "ABCD"
    assert log_entries[0]["name"] == "Alice tag"
    assert log_entries[0]["source"] == "nfc"


def test_homekey_user_name_resolution_by_endpoint_id_and_public_key(tmp_path):
    names_file = tmp_path / "homekey_names.json"
    names_file.write_text(
        json.dumps(
            {
                "endpoint_ids": {"ABCDEF123456": "Alice"},
                "public_keys": {"04AABBCC": "Bob"},
            }
        )
    )
    service = Service(
        FakeCLF(),
        FakeRepository(),
        homekey_user_names_path=str(names_file),
    )

    endpoint_by_id = FakeEndpoint("ABCDEF123456", "04112233")
    endpoint_by_public_key = FakeEndpoint("001122334455", "04AABBCC")

    assert service._get_homekey_user_name(endpoint_by_id) == "Alice"
    assert service._get_homekey_user_name(endpoint_by_public_key) == "Bob"


def test_add_and_remove_known_uid_via_management_helpers(tmp_path):
    known_file = tmp_path / "known.json"
    known_file.write_text(json.dumps({"uids": [{"uid": "AABB", "name": "Alice"}]}))
    service = Service(FakeCLF(), FakeRepository(), known_nfc_uids_path=str(known_file))

    assert service.add_known_nfc_uid("CC:DD", "Guest") is True
    assert service.add_known_nfc_uid("CCDD", "Guest") is False
    assert service.remove_known_nfc_uid("AABB") is True
    assert service.remove_known_nfc_uid("AABB") is False

    content = json.loads(known_file.read_text())
    assert content == {"uids": [{"uid": "CCDD", "name": "Guest"}]}


def test_add_and_remove_unknown_uid_via_management_helpers(tmp_path):
    unknown_file = tmp_path / "unknown.json"
    unknown_file.write_text(json.dumps({"uids": ["AABB"]}))
    service = Service(FakeCLF(), FakeRepository(), new_nfc_uids_path=str(unknown_file))

    assert service.add_unknown_nfc_uid("CC:DD") is True
    assert service.add_unknown_nfc_uid("CCDD") is False
    assert service.remove_unknown_nfc_uid("AABB") is True
    assert service.remove_unknown_nfc_uid("AABB") is False

    content = json.loads(unknown_file.read_text())
    assert content == {"uids": ["CCDD"]}


def test_remote_shell_command_disabled():
    service_disabled = Service(
        FakeCLF(),
        FakeRepository(),
        home_assistant_enable_shell_command=False,
        home_assistant_shell_command_whitelist=[],
    )
    assert service_disabled._is_remote_shell_command_allowed(["echo", "hello"]) is False


def test_remote_shell_command_allow_all():
    service_allow_all = Service(
        FakeCLF(),
        FakeRepository(),
        home_assistant_enable_shell_command=True,
        home_assistant_shell_command_whitelist=[],
    )
    assert service_allow_all._is_remote_shell_command_allowed(["echo", "hello"]) is True
    assert service_allow_all._is_remote_shell_command_allowed(["/bin/date"]) is True


def test_remote_shell_command_whitelist():
    service_whitelist = Service(
        FakeCLF(),
        FakeRepository(),
        home_assistant_enable_shell_command=True,
        home_assistant_shell_command_whitelist=["echo", "/usr/bin/python3"],
    )
    assert service_whitelist._is_remote_shell_command_allowed(["echo", "hello"]) is True
    assert service_whitelist._is_remote_shell_command_allowed(
        ["/usr/bin/python3", "--version"]
    ) is True
    assert service_whitelist._is_remote_shell_command_allowed(["date"]) is False
