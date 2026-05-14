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

    _discovered_host: str | None = None
    _discovered_port: int = DEFAULT_PORT
    _discovered_name: str | None = None

    async def async_step_zeroconf(self, discovery_info) -> FlowResult:
        host = str(getattr(discovery_info, "host", "") or "")
        if not host:
            return self.async_abort(reason="cannot_connect")
        port = int(getattr(discovery_info, "port", DEFAULT_PORT) or DEFAULT_PORT)
        name = str(getattr(discovery_info, "name", "") or "Apple Home Key Reader")

        await self.async_set_unique_id(f"{host}:{port}")
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: host, CONF_PORT: port}
        )

        self._discovered_host = host
        self._discovered_port = port
        self._discovered_name = name
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_user()

    async def async_step_user(self, user_input=None) -> FlowResult:
        errors: dict[str, str] = {}
        host_default = self._discovered_host or ""
        port_default = self._discovered_port or DEFAULT_PORT
        name_default = self._discovered_name or "Apple Home Key Reader"

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = int(user_input[CONF_PORT])
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured(
                updates={CONF_HOST: host, CONF_PORT: port}
            )
            api = AppleHomeKeyReaderApi(self.hass, user_input)
            try:
                if not await api.health():
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=user_input.get(CONF_NAME, name_default),
                        data=user_input,
                    )
            except AppleHomeKeyReaderError as exc:
                _LOGGER.debug("Connection validation failed: %s", exc)
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
