import base64
from datetime import datetime, timezone
import json
import logging
import os
import shlex
import subprocess
import time
from operator import attrgetter

from entity import (
    Issuer,
    Operation,
    ReaderKeyResponse,
    ReaderKeyRequest,
    HardwareFinishResponse,
    HardwareFinishColor,
    DeviceCredentialRequest,
    DeviceCredentialResponse,
    Endpoint,
    Enrollments,
    Enrollment,
    OperationStatus,
    SupportedConfigurationResponse,
    ControlPointRequest,
    ControlPointResponse,
)
from homekey import read_homekey, ProtocolError
from repository import Repository
from util.bfclf import (
    BroadcastFrameContactlessFrontend,
    RemoteTarget,
    activate,
    ISODEPTag,
)
from util.digital_key import DigitalKeyFlow, DigitalKeyTransactionType
from util.ecp import ECP
from util.iso7816 import ISO7816Tag
from util.threads import create_runner
from util.structable import pack_into_base64_string, unpack_from_base64_string

log = logging.getLogger()


class Service:
    UNCONFIGURED_READER_PRIVATE_KEY = bytes.fromhex("00" * 32)

    def __init__(
        self,
        clf: BroadcastFrameContactlessFrontend,
        repository: Repository,
        express: bool = True,
        finish: str = "silver",
        flow: str = "fast",
        throttle_polling=0.1,
        known_nfc_uids_path: str = "known_nfc_uids.json",
        new_nfc_uids_path: str = "new_nfc_uid.json",
        access_log_path: str = "access_log.jsonl",
        homekey_user_names_path: str = "homekey_user_names.json",
        on_known_nfc_shell_command: str = None,
        on_unknown_nfc_shell_command: str = None,
    ) -> None:
        self.repository = repository
        self.clf = clf
        self.throttle_polling = throttle_polling
        self.express = express in (True, "True", "true", "1")
        self.known_nfc_uids_path = known_nfc_uids_path
        self.new_nfc_uids_path = new_nfc_uids_path
        self.access_log_path = access_log_path
        self.homekey_user_names_path = homekey_user_names_path
        self.on_known_nfc_shell_command = on_known_nfc_shell_command
        self.on_unknown_nfc_shell_command = on_unknown_nfc_shell_command

        try:
            self.hardware_finish_color = HardwareFinishColor[finish.upper()]
        except KeyError:
            self.hardware_finish_color = HardwareFinishColor.BLACK
            log.warning(
                f"HardwareFinish {finish} is not supported. Falling back to {self.hardware_finish_color}"
            )
        try:
            self.flow = DigitalKeyFlow[flow.upper()]
        except KeyError:
            self.flow = DigitalKeyFlow.FAST
            log.warning(
                f"Digital Key flow {flow} is not supported. Falling back to {self.flow}"
            )

        self._run_flag = True
        self._runner = None

    def on_endpoint_authenticated(self, endpoint):
        """This method will be called when an endpoint is authenticated"""
        # Currently overwritten by accessory.py

    @staticmethod
    def _normalize_uid(uid):
        if uid is None:
            return None
        if isinstance(uid, bytes):
            return uid.hex().upper()
        uid = str(uid).strip().upper().replace(":", "").replace("-", "").replace(" ", "")
        if uid == "":
            return None
        return uid

    def _load_uid_list(self, path):
        if not path:
            return set()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return set()
        except Exception:
            log.exception(f'Could not parse NFC UID file "{path}"')
            return set()

        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            if isinstance(data.get("uids"), list):
                entries = data.get("uids", [])
            elif all(isinstance(k, str) for k in data.keys()):
                entries = list(data.keys())
            else:
                entries = []
        else:
            entries = []

        return {
            uid
            for uid in (self._normalize_uid(item) for item in entries)
            if uid is not None
        }

    def _load_known_nfc_uids_with_names(self):
        path = self.known_nfc_uids_path
        if not path:
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}
        except Exception:
            log.exception(f'Could not parse NFC UID file "{path}"')
            return {}

        entries = {}
        if isinstance(data, list):
            for entry in data:
                uid = self._normalize_uid(entry)
                if uid is not None:
                    entries[uid] = None
            return entries

        if isinstance(data, dict):
            if isinstance(data.get("uids"), list):
                for entry in data.get("uids", []):
                    if isinstance(entry, str):
                        uid = self._normalize_uid(entry)
                        if uid is not None:
                            entries[uid] = None
                    elif isinstance(entry, dict):
                        uid = self._normalize_uid(entry.get("uid"))
                        if uid is not None:
                            entries[uid] = (
                                str(entry.get("name")) if entry.get("name") else None
                            )
            for key, value in data.items():
                if key == "uids":
                    continue
                uid = self._normalize_uid(key)
                if uid is None:
                    continue
                if isinstance(value, str):
                    entries[uid] = value
                elif isinstance(value, dict):
                    entries[uid] = str(value.get("name")) if value.get("name") else None
                else:
                    entries[uid] = None
        return entries

    def _get_known_nfc_uid_name(self, uid):
        uid = self._normalize_uid(uid)
        if uid is None:
            return False, None
        known_uids = self._load_known_nfc_uids_with_names()
        if uid not in known_uids:
            return False, None
        return True, known_uids.get(uid)

    @staticmethod
    def _normalize_hex_id(value):
        if value is None:
            return None
        normalized = str(value).strip().upper().replace(":", "").replace("-", "").replace(" ", "")
        return normalized if normalized else None

    def _load_homekey_user_names(self):
        path = self.homekey_user_names_path
        if not path:
            return {}, {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}, {}
        except Exception:
            log.exception(f'Could not parse Home Key user names file "{path}"')
            return {}, {}

        if not isinstance(data, dict):
            return {}, {}

        endpoint_ids = data.get("endpoint_ids", data)
        public_keys = data.get("public_keys", {})
        if not isinstance(endpoint_ids, dict):
            endpoint_ids = {}
        if not isinstance(public_keys, dict):
            public_keys = {}

        endpoint_names = {}
        for key, value in endpoint_ids.items():
            key = self._normalize_hex_id(key)
            if key is None:
                continue
            endpoint_names[key] = str(value)

        public_key_names = {}
        for key, value in public_keys.items():
            key = self._normalize_hex_id(key)
            if key is None:
                continue
            public_key_names[key] = str(value)
        return endpoint_names, public_key_names

    def _get_homekey_user_name(self, endpoint):
        endpoint_names, public_key_names = self._load_homekey_user_names()
        endpoint_id = self._normalize_hex_id(endpoint.id.hex())
        if endpoint_id in endpoint_names:
            return endpoint_names.get(endpoint_id)

        public_key = self._normalize_hex_id(endpoint.public_key.hex())
        if public_key in public_key_names:
            return public_key_names.get(public_key)
        return None

    def _store_unknown_nfc_uid(self, uid):
        uid = self._normalize_uid(uid)
        if uid is None:
            return

        known_uids = self._load_uid_list(self.new_nfc_uids_path)
        if uid in known_uids:
            return
        known_uids.add(uid)
        with open(self.new_nfc_uids_path, "w", encoding="utf-8") as handle:
            json.dump(
                {"uids": sorted(known_uids)},
                handle,
                indent=2,
            )
        log.info(f'Stored unknown NFC UID "{uid}" in {self.new_nfc_uids_path}')

    def _append_access_log(self, *, event_type, source, uid=None, name=None, details=None):
        if self.access_log_path in (None, ""):
            return
        uid = self._normalize_uid(uid)
        event = {
            "at": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "source": source,
            "uid": uid,
            "name": name,
            "details": details or {},
        }
        with open(self.access_log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")

    def _run_shell_command(self, command, reason):
        if command in (None, ""):
            return
        log.info(f'Running shell command for "{reason}" event')
        try:
            command_args = command if isinstance(command, list) else shlex.split(command)
            if not command_args:
                return
            subprocess.Popen(
                command_args,
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            log.exception(
                f'Could not run shell command for "{reason}" event: {command}'
            )

    @staticmethod
    def _extract_uid(remote_target=None, target=None):
        if target is not None:
            identifier = getattr(target, "identifier", None)
            if identifier:
                return identifier.hex().upper()

        if remote_target is None:
            return None

        for attribute_name in ("sdd_res", "sensf_res", "sensb_res"):
            value = getattr(remote_target, attribute_name, None)
            if value:
                return bytes(value).hex().upper()
        return None

    def _handle_non_homekey_tag(self, uid):
        if uid is None:
            log.info("Found non-homekey NFC tag but could not extract UID")
            self._append_access_log(
                event_type="nfc_unknown",
                source="nfc",
                uid=uid,
                details={"reason": "uid-unavailable"},
            )
            return
        log.info(f"Found non-homekey NFC tag with UID: {uid}")
        is_known, key_name = self._get_known_nfc_uid_name(uid)
        if is_known:
            log.info(f'NFC UID "{uid}" is known')
            self._append_access_log(
                event_type="nfc_known",
                source="nfc",
                uid=uid,
                name=key_name,
            )
            self._run_shell_command(self.on_known_nfc_shell_command, "known-nfc")
            return
        log.info(f'NFC UID "{uid}" is unknown')
        self._append_access_log(
            event_type="nfc_unknown",
            source="nfc",
            uid=uid,
        )
        self._store_unknown_nfc_uid(uid)
        self._run_shell_command(self.on_unknown_nfc_shell_command, "unknown-nfc")

    def start(self):
        self._runner = create_runner(
            name="homekey",
            target=self.run,
            flag=attrgetter("_run_flag"),
            delay=0,
            exception_delay=5,
            start=True,
        )

    def stop(self):
        self._run_flag = False
        if self._runner is not None:
            self._runner.join()

    def update_hap_pairings(self, issuer_public_keys):
        issuers = {
            issuer.public_key: issuer for issuer in self.repository.get_all_issuers()
        }
        for issuer in issuers.values():
            if issuer.public_key in issuer_public_keys:
                continue
            log.info(f"Removing issuer {issuer} as their pairing has been removed")
            self.repository.remove_issuer(issuer)

        for issuer_public_key in issuer_public_keys:
            if issuer_public_key in issuers:
                continue
            issuer = Issuer(public_key=issuer_public_key, endpoints=[])
            log.info(f"Adding issuer {issuer} based on paired clients")
            self.repository.upsert_issuer(issuer)

    def _read_homekey(self):
        start = time.monotonic()

        remote_target = self.clf.sense(
            RemoteTarget("106A"),
            RemoteTarget("106B"),
            RemoteTarget("212F"),
            RemoteTarget("424F"),
            broadcast=ECP.home(
                identifier=self.repository.get_reader_group_identifier(),
                flag_2=self.express,
            ).pack(),
        )

        if remote_target is None:
            # Throttle polling attempts to prevent overheating & RF performance degradation
            time.sleep(max(0, self.throttle_polling - time.monotonic() + start))
            return

        target = activate(self.clf, remote_target)
        if target is None:
            return

        uid = self._extract_uid(remote_target, target)

        if not isinstance(target, ISODEPTag):
            self._handle_non_homekey_tag(uid)
            while self.clf.sense(
                RemoteTarget("106A"),
                RemoteTarget("106B"),
                RemoteTarget("212F"),
                RemoteTarget("424F"),
            ) is not None:
                log.info("Waiting for target to leave the field...")
                time.sleep(0.5)
            return

        log.info(f"Got NFC tag {target}")

        reader_private_key = self.repository.get_reader_private_key()
        endpoint = None
        attempted_homekey_auth = False
        if reader_private_key not in (
            None,
            b"",
            Service.UNCONFIGURED_READER_PRIVATE_KEY,
        ):
            attempted_homekey_auth = True
            tag = ISO7816Tag(target)
            try:
                result_flow, new_issuers_state, endpoint = read_homekey(
                    tag,
                    issuers=self.repository.get_all_issuers(),
                    preferred_versions=[b"\x02\x00"],
                    flow=self.flow,
                    transaction_code=DigitalKeyTransactionType.UNLOCK,
                    reader_identifier=self.repository.get_reader_group_identifier()
                    + self.repository.get_reader_identifier(),
                    reader_private_key=reader_private_key,
                    key_size=16,
                )

                if new_issuers_state is not None and len(new_issuers_state):
                    self.repository.upsert_issuers(new_issuers_state)

                log.info(f"Authenticated endpoint via {result_flow!r}: {endpoint}")

                end = time.monotonic()
                log.info(f"Transaction took {(end - start) * 1000} ms")

                if endpoint is not None:
                    endpoint_id = endpoint.id.hex().upper()
                    homekey_user_name = self._get_homekey_user_name(endpoint)
                    self._append_access_log(
                        event_type="homekey_authenticated",
                        source="homekey",
                        uid=uid,
                        name=homekey_user_name,
                        details={
                            "endpoint_id": endpoint_id,
                            "endpoint_public_key": endpoint.public_key.hex().upper(),
                            "flow": str(result_flow),
                        },
                    )
                    self._run_shell_command(
                        self.on_known_nfc_shell_command, "homekey-authenticated"
                    )
                    self.on_endpoint_authenticated(endpoint)
            except ProtocolError as e:
                log.info(f'Could not authenticate device due to protocol error "{e}"')
        else:
            log.info(
                "Home Key authentication is not configured yet, treating ISODEP tags as regular NFC tags"
            )

        if endpoint is None:
            if attempted_homekey_auth:
                log.info(
                    "ISODEP tag was not authenticated as Home Key, handling it as regular NFC tag"
                )
            self._handle_non_homekey_tag(uid)

        # Let device cool down, wait for ISODEP to drop to consider comms finished
        while target.is_present:
            log.info("Waiting for device to leave the field...")
            time.sleep(0.5)
        log.info("Device left the field. Continuing in 2 seconds...")
        time.sleep(2)
        log.info("Waiting for next device...")

    def run(self):
        log.info("Connecting to the NFC reader...")

        self.clf.device = None
        self.clf.open(self.clf.path)
        if self.clf.device is None:
            raise Exception(
                f"Could not connect to NFC device {self.clf} at {self.clf.path}"
            )

        while self._run_flag:
            self._read_homekey()

    def get_reader_key(self, request: ReaderKeyRequest) -> ReaderKeyResponse:
        response = ReaderKeyResponse(
            key_identifier=self.repository.get_reader_group_identifier(),
        )
        return response

    def add_reader_key(self, request: ReaderKeyRequest) -> ReaderKeyResponse:
        changed = False
        if self.repository.get_reader_private_key() != request.reader_private_key:
            changed = True
            self.repository.set_reader_private_key(request.reader_private_key)
        if self.repository.get_reader_identifier() != request.unique_reader_identifier:
            changed = True
            self.repository.set_reader_identifier(request.unique_reader_identifier)
        response = ReaderKeyResponse(
            status=OperationStatus.SUCCESS if changed else OperationStatus.DUPLICATE
        )
        return response

    def remove_reader_key(self, request: ReaderKeyRequest) -> ReaderKeyResponse:
        if request.key_identifier == self.repository.get_reader_group_identifier():
            self.repository.set_reader_private_key(bytes.fromhex("00" * 32))
        response = ReaderKeyResponse(
            status=OperationStatus.SUCCESS
            if request.key_identifier == self.repository.get_reader_group_identifier()
            else OperationStatus.DOES_NOT_EXIST
        )
        return response

    def get_device_credential(
        self, request: DeviceCredentialRequest
    ) -> DeviceCredentialResponse:
        log.info(f"*** get_device_credential request={request}")

    def add_device_credential(
        self, request: DeviceCredentialRequest
    ) -> DeviceCredentialResponse:
        endpoint = self.repository.get_endpoint_by_public_key(
            b"\x04" + request.credential_public_key
        )
        log.info(f"*** add_device_credential endpoint={endpoint}")

        if endpoint is not None:
            if endpoint.enrollments.hap is None:
                issuer = self.repository.get_issuer_by_id(request.issuer_key_identifier)
                endpoint.enrollments.hap = Enrollment(
                    at=int(time.time()),
                    payload=base64.b64encode(request.pack()).decode(),
                )
                self.repository.upsert_endpoint(issuer.id, endpoint)
            return DeviceCredentialResponse(
                key_identifier=self.repository.get_reader_group_identifier(),
                status=OperationStatus.DUPLICATE,
            )

        issuer = self.repository.get_issuer_by_id(request.issuer_key_identifier)
        log.info(f"*** add_device_credential issuer={issuer}")

        if issuer is None:
            return DeviceCredentialResponse(
                key_identifier=self.repository.get_reader_group_identifier(),
                status=OperationStatus.DOES_NOT_EXIST,
            )

        self.repository.upsert_endpoint(
            issuer.id,
            Endpoint(
                last_used_at=0,
                counter=0,
                key_type=request.key_type,
                public_key=b"\x04" + request.credential_public_key,
                persistent_key=os.urandom(32),
                enrollments=Enrollments(
                    hap=Enrollment(
                        at=int(time.time()),
                        payload=base64.b64encode(request.pack()).decode(),
                    ),
                    attestation=None,
                ),
            ),
        )
        return DeviceCredentialResponse(
            issuer_key_identifier=issuer.id, status=OperationStatus.DUPLICATE
        )

    def remove_device_credential(
        self, request: DeviceCredentialRequest
    ) -> DeviceCredentialResponse:
        log.info(f"*** remove_device_credential request={request}")

    def get_hardware_finish(self):
        result = pack_into_base64_string(
            HardwareFinishResponse(color=self.hardware_finish_color)
        )
        log.info(f"get_hardware_finish={result}")
        return result

    def get_nfc_access_supported_configuration(self):
        result = pack_into_base64_string(
            SupportedConfigurationResponse(
                number_of_issuer_keys=16, number_of_inactive_credentials=16
            )
        )
        log.info(f"TODO get_nfc_access_supported_configuration={result}")
        return result

    def get_nfc_access_control_point(self):
        log.info("get_nfc_access_control_point")
        return ""

    def set_nfc_access_control_point(self, value):
        log.info(f"<-- (B64) {value}")
        request_packed_tlv = unpack_from_base64_string(value)
        log.info(f"<-- (TLV) {request_packed_tlv.hex()}")
        request: ControlPointRequest = ControlPointRequest.unpack(request_packed_tlv)
        log.info(f"<-- (OBJ) {request}")
        operation = request.operation
        response = ControlPointResponse()

        if request.device_credential_request is not None:
            subrequest: DeviceCredentialRequest = request.device_credential_request
            response.device_credential_response = (
                self.get_device_credential(subrequest)
                if operation == Operation.GET
                else self.add_device_credential(subrequest)
                if operation == Operation.ADD
                else self.remove_device_credential(subrequest)
                if operation == Operation.REMOVE
                else None
            )
        elif request.reader_key_request is not None:
            subrequest: ReaderKeyRequest = request.reader_key_request
            response.reader_key_response = (
                self.get_reader_key(subrequest)
                if operation == Operation.GET
                else self.add_reader_key(subrequest)
                if operation == Operation.ADD
                else self.remove_reader_key(subrequest)
                if operation == Operation.REMOVE
                else None
            )
        log.info(f"--> (OBJ) {response}")
        packed_tlv_response = response.pack()
        log.info(f"--> (TLV) {packed_tlv_response.hex()}")
        response = pack_into_base64_string(packed_tlv_response)
        log.info(f"--> (B64) {response}")
        return response

    def get_configuration_state(self):
        log.info("get_configuration_state")
        return 0
