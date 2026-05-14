from __future__ import annotations

from typing import Any

from aiohttp import ClientError
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import async_timeout

from .const import CONF_BASE_PATH, CONF_TOKEN, DEFAULT_TIMEOUT_SECONDS


class AppleHomeKeyReaderApi:
    def __init__(self, hass, config: dict[str, Any]) -> None:
        self._hass = hass
        self._host = config[CONF_HOST]
        self._port = config[CONF_PORT]
        self._token = config.get(CONF_TOKEN, "")
        base_path = str(config.get(CONF_BASE_PATH, "/ha") or "/ha").strip()
        self._base_path = base_path if base_path.startswith("/") else f"/{base_path}"

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    @property
    def headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    async def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        session = async_get_clientsession(self._hass)
        try:
            async with async_timeout.timeout(DEFAULT_TIMEOUT_SECONDS):
                async with session.request(
                    method,
                    url,
                    headers=self.headers,
                    json=payload,
                ) as response:
                    data = await response.json(content_type=None)
                    if response.status >= 400:
                        raise AppleHomeKeyReaderError(
                            f"HTTP {response.status} for {path}: {data}"
                        )
                    return data if isinstance(data, dict) else {"ok": False}
        except (ClientError, TimeoutError) as exc:
            raise AppleHomeKeyReaderError(f"Request failed: {exc}") from exc

    async def health(self) -> bool:
        data = await self.request("GET", f"{self._base_path}/health")
        return bool(data.get("ok"))

    async def run_known_shell_command(self) -> dict:
        return await self.request("POST", f"{self._base_path}/run-known-shell-command")

    async def add_known_uid(self, uid: str, name: str | None = None) -> dict:
        payload = {"uid": uid}
        if name not in (None, ""):
            payload["name"] = name
        return await self.request("POST", f"{self._base_path}/nfc/known/add", payload)

    async def remove_known_uid(self, uid: str) -> dict:
        return await self.request(
            "POST",
            f"{self._base_path}/nfc/known/remove",
            {"uid": uid},
        )

    async def add_unknown_uid(self, uid: str) -> dict:
        return await self.request(
            "POST",
            f"{self._base_path}/nfc/unknown/add",
            {"uid": uid},
        )

    async def remove_unknown_uid(self, uid: str) -> dict:
        return await self.request(
            "POST",
            f"{self._base_path}/nfc/unknown/remove",
            {"uid": uid},
        )

    async def run_shell_command(self, command: list[str]) -> dict:
        return await self.request(
            "POST",
            f"{self._base_path}/shell/run",
            {"command": command},
        )


class AppleHomeKeyReaderError(Exception):
    pass

