from __future__ import annotations

import logging

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
import voluptuous as vol

from .api import AppleHomeKeyReaderApi, AppleHomeKeyReaderError
from .const import CONF_BASE_PATH, CONF_TOKEN, DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)


class AppleHomeKeyReaderConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._port: int = DEFAULT_PORT
        self._name: str | None = None

    def is_matching(self, other_flow: object) -> bool:
        if self._host is None:
            return False
        other_host = getattr(other_flow, "_host", None)
        other_port = getattr(other_flow, "_port", None)
        return self._host == other_host and self._port == other_port

    async def async_step_zeroconf(self, discovery_info) -> FlowResult:
        host = str(getattr(discovery_info, "host", "") or "")
        if not host:
            _LOGGER.debug("Zeroconf discovery aborted: missing host in discovery_info")
            return self.async_abort(reason="cannot_connect")
        port = int(getattr(discovery_info, "port", DEFAULT_PORT) or DEFAULT_PORT)
        name = str(getattr(discovery_info, "name", "") or "Apple Home Key Reader")
        _LOGGER.debug(
            "Zeroconf discovery received host=%s port=%s name=%s", host, port, name
        )

        self._host = host
        self._port = port
        self._name = name

        await self.async_set_unique_id(f"{host}:{port}")
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: host, CONF_PORT: port}
        )

        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_user()

    async def async_step_user(self, user_input=None) -> FlowResult:
        errors: dict[str, str] = {}
        host_default = self._host or ""
        port_default = self._port or DEFAULT_PORT
        name_default = self._name or "Apple Home Key Reader"

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = int(user_input[CONF_PORT])
            base_path = str(user_input.get(CONF_BASE_PATH, "/ha") or "/ha")
            normalized_base_path = (
                base_path if base_path.startswith("/") else f"/{base_path}"
            )
            health_url = f"http://{host}:{port}{normalized_base_path}/health"

            self._host = host
            self._port = port
            self._name = user_input.get(CONF_NAME, name_default)

            await self.async_set_unique_id(f"{host}:{port}", raise_on_progress=False)
            self._abort_if_unique_id_configured(
                updates={CONF_HOST: host, CONF_PORT: port}
            )
            _LOGGER.debug(
                (
                    "Validating connection to host=%s port=%s base_path=%s "
                    "health_url=%s token_set=%s"
                ),
                host,
                port,
                base_path,
                health_url,
                bool(user_input.get(CONF_TOKEN)),
            )
            api = AppleHomeKeyReaderApi(self.hass, user_input)
            try:
                if not await api.health():
                    _LOGGER.debug(
                        (
                            "Health endpoint returned a non-ok payload for "
                            "host=%s port=%s health_url=%s"
                        ),
                        host,
                        port,
                        health_url,
                    )
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=self._name,
                        data=user_input,
                    )
            except AppleHomeKeyReaderError as exc:
                _LOGGER.debug(
                    (
                        "Connection validation failed for host=%s port=%s "
                        "health_url=%s error=%s"
                    ),
                    host,
                    port,
                    health_url,
                    exc,
                    exc_info=True,
                )
                errors["base"] = "cannot_connect"

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=name_default): selector.TextSelector(),
                vol.Required(CONF_HOST, default=host_default): selector.TextSelector(),
                vol.Required(CONF_PORT, default=port_default): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=65535,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(CONF_TOKEN, default=""): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_BASE_PATH, default="/ha"): selector.TextSelector(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
