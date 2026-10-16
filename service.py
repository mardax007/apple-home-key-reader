import base64
from collections import namedtuple
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import shlex
import signal
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from operator import attrgetter
from threading import Lock, Thread
from zeroconf import ServiceInfo, Zeroconf

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

# One reserved door run; token tells a run's own exit apart from a stale one
UnlockRun = namedtuple("UnlockRun", ["token", "actor_key", "started_at"])


class Service:
    UNCONFIGURED_READER_PRIVATE_KEY = bytes.fromhex("00" * 32)
    HTTP_SHUTDOWN_TIMEOUT = 2
    HTTP_SERVER_POLL_INTERVAL = 0.5
    HOME_ASSISTANT_SERVICE_TYPE = "_apple-home-key-reader._tcp.local."
    DEFAULT_SHELL_COMMAND_WORKING_DIRECTORY = os.path.dirname(
        os.path.abspath(__file__)
    )
    # Ignore the same actor triggering again this soon after its run ended (e.g. a card left on the reader)
    UNLOCK_REPEAT_SUPPRESS_SECONDS = 5
    # An unlock command still running after this is killed
    UNLOCK_MAX_RUN_SECONDS = 30
    # Backstop if a run's exit is never reported (e.g. its monitor thread could not start)
    UNLOCK_SLOT_EXPIRY_SECONDS = UNLOCK_MAX_RUN_SECONDS + 10

    @staticmethod
    def _parse_bool(value, default=False):
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        value = str(value).strip().lower()
        if value == "":
            return default
        return value in ("1", "true", "yes", "on")

    @staticmethod
    def _normalize_shell_command_value(command):
        if command is None:
            return None
        if isinstance(command, str):
            command = command.strip()
            return command if command else None
        if isinstance(command, list):
            command = [str(item).strip() for item in command if str(item).strip()]
            return command if command else None
        return command

    def __init__(
        self,
        clf: BroadcastFrameContactlessFrontend,
        repository: Repository,
        express: bool = True,
        finish: str = "silver",
        flow: str = "fast",
        throttle_polling=0.1,
        known_nfc_uids_path: str = "known_nfc_uids.json",
        new_nfc_uids_path: str = "new_nfc_uids.json",
        access_log_path: str = "access_log.jsonl",
        homekey_user_names_path: str = "homekey_user_names.json",
        on_unlock_shell_command: str = None,
        on_known_nfc_shell_command: str = None,
        on_unknown_nfc_shell_command: str = None,
        home_assistant_enabled: bool = False,
        home_assistant_host: str = "0.0.0.0",
        home_assistant_port: int = 9780,
        home_assistant_token: str = None,
        shell_command_working_directory: str = None,
    ) -> None:
        self.repository = repository
        self.clf = clf
        self.throttle_polling = throttle_polling
        self.express = self._parse_bool(express, default=True)
        self.known_nfc_uids_path = known_nfc_uids_path
        self.new_nfc_uids_path = new_nfc_uids_path
        self.access_log_path = access_log_path
        self.homekey_user_names_path = homekey_user_names_path
        # Empty/None unlock command falls back to legacy on_known command for compatibility.
        normalized_unlock_command = self._normalize_shell_command_value(
            on_unlock_shell_command
        )
        self.on_known_nfc_shell_command = self._normalize_shell_command_value(
            on_known_nfc_shell_command
        )
        self.on_unknown_nfc_shell_command = self._normalize_shell_command_value(
            on_unknown_nfc_shell_command
        )
        self.on_unlock_shell_command = (
            normalized_unlock_command
            if normalized_unlock_command is not None
            else self.on_known_nfc_shell_command
        )
        self.home_assistant_enabled = self._parse_bool(home_assistant_enabled)
        self.home_assistant_host = home_assistant_host
        self.home_assistant_port = int(home_assistant_port)
        self.home_assistant_token = home_assistant_token
        if isinstance(shell_command_working_directory, str):
            shell_command_working_directory = shell_command_working_directory.strip()
        self.shell_command_working_directory = (
            shell_command_working_directory
            if shell_command_working_directory
            else self.DEFAULT_SHELL_COMMAND_WORKING_DIRECTORY
        )
        if (
            self.shell_command_working_directory is not None
            and not os.path.isdir(self.shell_command_working_directory)
        ):
            log.warning(
                f'Shell command working directory "{self.shell_command_working_directory}" does not exist. '
                "Falling back to current process working directory."
            )
            self.shell_command_working_directory = None

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
        self._home_assistant_httpd = None
        self._home_assistant_thread = None
        self._home_assistant_zeroconf = None
        self._home_assistant_service_info = None
        self._nfc_uids_lock = Lock()
        # Only one unlock command may drive the door at a time
        self._unlock_slot_lock = Lock()
        self._unlock_run = None
        self._unlock_run_counter = 0
        self._last_unlock_exit_at_by_actor = {}
        # Small bounded pool prevents unbounded thread creation if many tags are read quickly;
        # waiting on process exits is lightweight and 2 workers is sufficient here.
        # It is shut down in stop() with wait=False to avoid blocking shutdown on long scripts.
        self._shell_command_monitor_pool = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="shell-command-monitor"
        )

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
                            name = entry.get("name")
                            entries[uid] = str(name) if name else None
            for key, value in data.items():
                if key == "uids":
                    continue
                uid = self._normalize_uid(key)
                if uid is None:
                    continue
                if isinstance(value, str):
                    entries[uid] = value
                elif isinstance(value, dict):
                    name = value.get("name")
                    entries[uid] = str(name) if name else None
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

        with self._nfc_uids_lock:
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

    def add_known_nfc_uid(self, uid, name=None):
        uid = self._normalize_uid(uid)
        if uid is None:
            return False
        with self._nfc_uids_lock:
            entries = self._load_known_nfc_uids_with_names()
            if name is None:
                next_name = None
            else:
                stripped_name = str(name).strip()
                next_name = stripped_name if stripped_name else None
            changed = uid not in entries or entries.get(uid) != next_name
            entries[uid] = next_name
            self._save_known_nfc_uids_with_names(entries)
        return changed

    def remove_known_nfc_uid(self, uid):
        uid = self._normalize_uid(uid)
        if uid is None:
            return False
        with self._nfc_uids_lock:
            entries = self._load_known_nfc_uids_with_names()
            if uid not in entries:
                return False
            del entries[uid]
            self._save_known_nfc_uids_with_names(entries)
        return True

    def add_unknown_nfc_uid(self, uid):
        uid = self._normalize_uid(uid)
        if uid is None:
            return False
        with self._nfc_uids_lock:
            known_uids = self._load_uid_list(self.new_nfc_uids_path)
            if uid in known_uids:
                return False
            known_uids.add(uid)
            self._save_uid_list(self.new_nfc_uids_path, known_uids)
        return True

    def remove_unknown_nfc_uid(self, uid):
        uid = self._normalize_uid(uid)
        if uid is None:
            return False
        with self._nfc_uids_lock:
            known_uids = self._load_uid_list(self.new_nfc_uids_path)
            if uid not in known_uids:
                return False
            known_uids.remove(uid)
            self._save_uid_list(self.new_nfc_uids_path, known_uids)
        return True

    @staticmethod
    def _save_uid_list(path, values):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"uids": sorted(values)}, handle, indent=2)

    def _save_known_nfc_uids_with_names(self, entries):
        payload = {
            "uids": [
                {"uid": uid, "name": name}
                if name is not None
                else uid
                for uid, name in sorted(entries.items())
            ]
        }
        with open(self.known_nfc_uids_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

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

    def _run_shell_command(self, command, reason, on_exit=None, timeout_seconds=None):
        if command in (None, ""):
            return False
        log.info(f'Running shell command for "{reason}" event')
        try:
            command_args = self._prepare_shell_command_args(command)
            if not command_args:
                return False
            process = subprocess.Popen(
                command_args,
                shell=False,
                cwd=self.shell_command_working_directory,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                if on_exit is None:
                    self._shell_command_monitor_pool.submit(
                        self._wait_for_shell_command_exit, process, reason, command_args
                    )
                else:
                    # Own thread so the exit handler never queues behind other commands
                    # and still runs after stop() shuts the shared pool down.
                    Thread(
                        target=self._wait_for_shell_command_exit,
                        args=(process, reason, command_args, on_exit, timeout_seconds),
                        name=f"shell-command-exit-{reason}",
                        daemon=True,
                    ).start()
            except Exception:
                log.exception(
                    f'Could not start shell command monitor for "{reason}" event: {command_args}'
                )
                if on_exit is not None:
                    # Nobody would report this run's exit, so stop it and report it as not started
                    self._kill_process_group(process)
                    return False
            return True
        except Exception:
            log.exception(
                f'Could not run shell command for "{reason}" event: {command}'
            )
            return False

    def _wait_for_shell_command_exit(
        self, process, reason, command_args, on_exit=None, timeout_seconds=None
    ):
        try:
            if timeout_seconds is None:
                returncode = process.wait()
            else:
                try:
                    returncode = process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    log.error(
                        f'Shell command for "{reason}" event still running after {timeout_seconds} s, killing it: {command_args}'
                    )
                    self._kill_process_group(process)
                    returncode = process.wait()
            if returncode != 0:
                log.error(
                    f'Shell command for "{reason}" event failed with exit code {returncode}: {command_args}'
                )
        except Exception:
            log.exception(
                f'Could not monitor shell command for "{reason}" event: {command_args}'
            )
        finally:
            if on_exit is not None:
                try:
                    on_exit()
                except Exception:
                    log.exception(f'Exit handler for "{reason}" event failed')

    @staticmethod
    def _kill_process_group(process):
        # Commands start in their own session, so on POSIX this also stops their children
        try:
            if hasattr(os, "killpg"):
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        except Exception:
            log.exception(f"Could not kill process {getattr(process, 'pid', None)}")

    @staticmethod
    def _prepare_shell_command_args(command):
        if isinstance(command, list):
            return [str(item).strip() for item in command if str(item).strip()]
        if isinstance(command, str):
            return [item for item in shlex.split(command) if item]
        return []

    def run_unlock_shell_command(
        self, reason, uid=None, name=None, actor=None, actor_type=None, source="unlock", details=None
    ):
        token, skip_reason = self._claim_unlock_slot(source, actor)
        if skip_reason is None:
            command_started = self._run_shell_command(
                self.on_unlock_shell_command,
                reason,
                on_exit=lambda: self._on_unlock_command_exit(token),
                timeout_seconds=self.UNLOCK_MAX_RUN_SECONDS,
            )
            if not command_started:
                self._release_unlock_slot(token)
        else:
            command_started = False
            log.info(f'Skipping unlock for "{reason}" event: {skip_reason}')
        unlock_details = dict(details or {})
        unlock_details["reason"] = reason
        unlock_details["command_started"] = command_started
        if skip_reason is not None:
            unlock_details["skipped"] = skip_reason
        if actor is not None:
            unlock_details["actor"] = actor
        if actor_type is not None:
            unlock_details["actor_type"] = actor_type
        self._append_access_log(
            event_type="door_unlocked" if skip_reason is None else "door_unlock_skipped",
            source=source,
            uid=uid,
            name=name,
            details=unlock_details,
        )
        return command_started

    def _is_unlock_slot_busy(self, now):
        return (
            self._unlock_run is not None
            and now - self._unlock_run.started_at < self.UNLOCK_SLOT_EXPIRY_SECONDS
        )

    def _claim_unlock_slot(self, source, actor):
        """Reserve the door for one unlock run.

        Returns (token, None) when reserved, or (None, skip_reason) when not.
        """
        now = time.monotonic()
        with self._unlock_slot_lock:
            if self._is_unlock_slot_busy(now):
                return None, "already-running"
            recent = {
                key: at
                for key, at in self._last_unlock_exit_at_by_actor.items()
                if now - at < self.UNLOCK_REPEAT_SUPPRESS_SECONDS
            }
            self._last_unlock_exit_at_by_actor = recent
            key = (source, actor)
            if key in recent:
                return None, "repeat-trigger"
            self._unlock_run_counter += 1
            self._unlock_run = UnlockRun(
                token=self._unlock_run_counter, actor_key=key, started_at=now
            )
            return self._unlock_run.token, None

    def _release_unlock_slot(self, token):
        with self._unlock_slot_lock:
            if self._unlock_run is not None and self._unlock_run.token == token:
                self._unlock_run = None

    def is_unlock_command_running(self):
        with self._unlock_slot_lock:
            return self._is_unlock_slot_busy(time.monotonic())

    def call_if_unlock_running(self, action):
        """Run action while holding the unlock slot, only if an unlock is running."""
        with self._unlock_slot_lock:
            if not self._is_unlock_slot_busy(time.monotonic()):
                return False
            action()
            return True

    def call_if_unlock_idle(self, action):
        """Run action while holding the unlock slot, only if no unlock is running."""
        with self._unlock_slot_lock:
            if self._is_unlock_slot_busy(time.monotonic()):
                return False
            action()
            return True

    def _on_unlock_command_exit(self, token):
        with self._unlock_slot_lock:
            run = self._unlock_run
            if run is not None and run.token != token:
                # A newer run owns the door now; leave its slot and lock state alone
                log.warning(f"Ignoring exit of stale unlock run {token}")
                return
            if run is not None:
                self._unlock_run = None
                self._last_unlock_exit_at_by_actor = {
                    **self._last_unlock_exit_at_by_actor,
                    run.actor_key: time.monotonic(),
                }
            # With no run owning the door (e.g. this run's slot expired), re-locking is still correct
            # Re-lock under the slot lock so a new unlock cannot interleave with it
            callback = getattr(self, "on_unlock_shell_command_complete", None)
            if callable(callback):
                callback()

    @staticmethod
    def _read_json_body(request):
        try:
            length = int(request.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = request.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def _is_home_assistant_request_authorized(self, headers):
        token = self.home_assistant_token
        if token in (None, ""):
            return True
        auth_header = headers.get("Authorization", "")
        if auth_header == f"Bearer {token}":
            return True
        return headers.get("X-HA-Token") == token

    @staticmethod
    def _write_home_assistant_response(request, code, payload):
        body = json.dumps(payload).encode("utf-8")
        request.send_response(code)
        request.send_header("Content-Type", "application/json")
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    def _build_home_assistant_handler(self):
        service = self

        class HomeAssistantHandler(BaseHTTPRequestHandler):
            def log_message(self, msg_format, *args):
                log.debug(f"home-assistant-api {msg_format % args}")

            def do_GET(self):
                if not service._is_home_assistant_request_authorized(self.headers):
                    return service._write_home_assistant_response(
                        self, 401, {"ok": False, "error": "unauthorized"}
                    )
                if self.path == "/ha/nfc/known/list":
                    return service._write_home_assistant_response(
                        self,
                        200,
                        {"ok": True, "uids": service.list_known_nfc_uids()},
                    )
                if self.path == "/ha/nfc/unknown/list":
                    return service._write_home_assistant_response(
                        self,
                        200,
                        {"ok": True, "uids": service.list_unknown_nfc_uids()},
                    )
                if self.path != "/ha/health":
                    return service._write_home_assistant_response(
                        self, 404, {"ok": False, "error": "not-found"}
                    )
                return service._write_home_assistant_response(self, 200, {"ok": True})

            def do_POST(self):
                if not service._is_home_assistant_request_authorized(self.headers):
                    return service._write_home_assistant_response(
                        self, 401, {"ok": False, "error": "unauthorized"}
                    )
                payload = service._read_json_body(self)
                if payload is None:
                    return service._write_home_assistant_response(
                        self, 400, {"ok": False, "error": "invalid-json"}
                    )
                path = self.path
                if path == "/ha/unlock":
                    service.run_unlock_shell_command(
                        "home-assistant-button",
                        actor="home-assistant",
                        actor_type="integration",
                        source="home-assistant",
                    )
                    return service._write_home_assistant_response(self, 200, {"ok": True})
                if path == "/ha/nfc/known/add":
                    changed = service.add_known_nfc_uid(
                        payload.get("uid"), payload.get("name")
                    )
                    return service._write_home_assistant_response(
                        self, 200, {"ok": True, "changed": changed}
                    )
                if path == "/ha/nfc/known/remove":
                    changed = service.remove_known_nfc_uid(payload.get("uid"))
                    return service._write_home_assistant_response(
                        self, 200, {"ok": True, "changed": changed}
                    )
                if path == "/ha/nfc/unknown/add":
                    changed = service.add_unknown_nfc_uid(payload.get("uid"))
                    return service._write_home_assistant_response(
                        self, 200, {"ok": True, "changed": changed}
                    )
                if path == "/ha/nfc/unknown/remove":
                    changed = service.remove_unknown_nfc_uid(payload.get("uid"))
                    return service._write_home_assistant_response(
                        self, 200, {"ok": True, "changed": changed}
                    )
                return service._write_home_assistant_response(
                    self, 404, {"ok": False, "error": "not-found"}
                )

        return HomeAssistantHandler

    def _start_home_assistant_api(self):
        if not self.home_assistant_enabled:
            return
        if self.home_assistant_token in (None, ""):
            log.warning(
                "Home Assistant API is enabled without a token; requests will be unauthenticated"
            )
        try:
            handler_cls = self._build_home_assistant_handler()
            self._home_assistant_httpd = ThreadingHTTPServer(
                (self.home_assistant_host, self.home_assistant_port), handler_cls
            )
            self._home_assistant_thread = Thread(
                target=self._home_assistant_httpd.serve_forever,
                kwargs={"poll_interval": self.HTTP_SERVER_POLL_INTERVAL},
                daemon=True,
            )
            self._home_assistant_thread.start()
            log.info(
                "Started Home Assistant API server at "
                f"http://{self.home_assistant_host}:{self.home_assistant_port}"
            )
            self._start_home_assistant_discovery()
        except Exception:
            log.exception("Could not start Home Assistant API server")

    def _stop_home_assistant_api(self):
        self._stop_home_assistant_discovery()
        if self._home_assistant_httpd is not None:
            self._home_assistant_httpd.shutdown()
            self._home_assistant_httpd.server_close()
            self._home_assistant_httpd = None
        if self._home_assistant_thread is not None:
            self._home_assistant_thread.join(
                timeout=self.HTTP_SHUTDOWN_TIMEOUT
            )
            if self._home_assistant_thread.is_alive():
                log.warning(
                    "Home Assistant API server thread did not stop within timeout; some resources may remain open"
                )
            self._home_assistant_thread = None

    @staticmethod
    def _resolve_local_ip():
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            ip = probe.getsockname()[0]
        except Exception:
            ip = None
        finally:
            probe.close()
        if ip not in (None, "", "0.0.0.0") and not ip.startswith("127."):
            return ip
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            return None
        if ip in ("", "0.0.0.0") or ip.startswith("127."):
            return None
        return ip

    def _start_home_assistant_discovery(self):
        advertised_ip = self._resolve_local_ip()
        if advertised_ip is None:
            log.warning(
                "Could not resolve a local network IP for Home Assistant discovery"
            )
            return
        try:
            instance_name = f"apple-home-key-reader-{socket.gethostname()}"
            self._home_assistant_service_info = ServiceInfo(
                self.HOME_ASSISTANT_SERVICE_TYPE,
                f"{instance_name}.{self.HOME_ASSISTANT_SERVICE_TYPE}",
                addresses=[socket.inet_aton(advertised_ip)],
                port=self.home_assistant_port,
                properties={
                    "name": "Apple Home Key Reader",
                    "host": advertised_ip,
                    "api_path": "/ha/health",
                    "version": "1",
                },
                server=f"{instance_name}.local.",
            )
            self._home_assistant_zeroconf = Zeroconf()
            self._home_assistant_zeroconf.register_service(
                self._home_assistant_service_info,
                strict=False,
            )
            log.info(
                "Published Home Assistant discovery service via mDNS at "
                f"{advertised_ip}:{self.home_assistant_port}"
            )
        except Exception:
            log.exception("Could not publish Home Assistant discovery service")
            self._home_assistant_service_info = None
            if self._home_assistant_zeroconf is not None:
                try:
                    self._home_assistant_zeroconf.close()
                except Exception:
                    pass
                self._home_assistant_zeroconf = None

    def _stop_home_assistant_discovery(self):
        if (
            self._home_assistant_zeroconf is not None
            and self._home_assistant_service_info is not None
        ):
            try:
                self._home_assistant_zeroconf.unregister_service(
                    self._home_assistant_service_info
                )
            except Exception:
                log.exception("Could not unpublish Home Assistant discovery service")
        if self._home_assistant_zeroconf is not None:
            try:
                self._home_assistant_zeroconf.close()
            except Exception:
                pass
            self._home_assistant_zeroconf = None
        self._home_assistant_service_info = None

    def list_known_nfc_uids(self):
        with self._nfc_uids_lock:
            entries = self._load_known_nfc_uids_with_names()
        return [
            {"uid": uid, "name": name}
            for uid, name in sorted(entries.items(), key=lambda item: item[0])
        ]

    def list_unknown_nfc_uids(self):
        with self._nfc_uids_lock:
            values = self._load_uid_list(self.new_nfc_uids_path)
        return sorted(values)

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
            self.run_unlock_shell_command(
                "known-nfc",
                uid=uid,
                name=key_name,
                actor=key_name if key_name is not None else uid,
                actor_type="nfc-uid",
                source="nfc",
            )
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
        self._start_home_assistant_api()
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
        self._stop_home_assistant_api()
        # Don't block shutdown on long-running commands; queued monitor tasks are canceled.
        self._shell_command_monitor_pool.shutdown(wait=False, cancel_futures=True)
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
                    self.run_unlock_shell_command(
                        "homekey-authenticated",
                        uid=uid,
                        name=homekey_user_name,
                        actor=(
                            homekey_user_name
                            if homekey_user_name is not None
                            else f"endpoint:{endpoint_id}"
                        ),
                        actor_type="homekey-endpoint",
                        source="homekey",
                        details={
                            "endpoint_id": endpoint_id,
                            "flow": str(result_flow),
                        },
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
