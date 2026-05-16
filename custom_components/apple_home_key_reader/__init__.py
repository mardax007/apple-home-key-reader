from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
import voluptuous as vol

from .api import AppleHomeKeyReaderApi, AppleHomeKeyReaderError
from .const import (
    DOMAIN,
    SERVICE_ADD_KNOWN_UID,
    SERVICE_ADD_UNKNOWN_UID,
    SERVICE_LIST_KNOWN_UIDS,
    SERVICE_LIST_UNKNOWN_UIDS,
    SERVICE_REMOVE_KNOWN_UID,
    SERVICE_REMOVE_UNKNOWN_UID,
    SERVICE_RUN_KNOWN_SHELL_COMMAND,
    SERVICE_RUN_SHELL_COMMAND,
    SERVICE_UNLOCK,
)
from .coordinator import AppleHomeKeyReaderCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["select", "button"]

CONF_ENTRY_ID = "entry_id"
CONF_COMMAND = "command"
CONF_UID = "uid"

DATA_APIS = "apis"
DATA_REGISTERED = "registered"


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(DATA_APIS, {})
    hass.data[DOMAIN].setdefault("entries", {})
    if hass.data[DOMAIN].get(DATA_REGISTERED):
        return True

    base_schema = vol.Schema({vol.Optional(CONF_ENTRY_ID): str})
    uid_schema = vol.Schema(
        {
            vol.Required(CONF_UID): str,
            vol.Optional(CONF_NAME): str,
            vol.Optional(CONF_ENTRY_ID): str,
        }
    )
    uid_remove_schema = vol.Schema(
        {
            vol.Required(CONF_UID): str,
            vol.Optional(CONF_ENTRY_ID): str,
        }
    )
    shell_schema = vol.Schema(
        {
            vol.Required(CONF_COMMAND): vol.Any(str, [str]),
            vol.Optional(CONF_ENTRY_ID): str,
        }
    )

    async def _resolve_api(call: ServiceCall) -> AppleHomeKeyReaderApi:
        apis = hass.data[DOMAIN][DATA_APIS]
        if not apis:
            raise HomeAssistantError("No Apple Home Key Reader config entries found")
        entry_id = call.data.get(CONF_ENTRY_ID)
        if entry_id:
            api = apis.get(entry_id)
            if api is None:
                raise HomeAssistantError(f"Unknown entry_id: {entry_id}")
            return api
        return next(iter(apis.values()))

    async def _call_api(call: ServiceCall, method_name: str) -> dict:
        api = await _resolve_api(call)
        method = getattr(api, method_name)
        try:
            if method_name == "add_known_uid":
                return await method(call.data[CONF_UID], call.data.get(CONF_NAME))
            elif method_name in ("remove_known_uid", "add_unknown_uid", "remove_unknown_uid"):
                return await method(call.data[CONF_UID])
            elif method_name == "run_shell_command":
                return await method(call.data[CONF_COMMAND])
            else:
                return await method()
        except AppleHomeKeyReaderError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def _unlock(call: ServiceCall):
        return await _call_api(call, "unlock")

    async def _run_known_shell_command(call: ServiceCall):
        return await _call_api(call, "run_known_shell_command")

    async def _add_known_uid(call: ServiceCall):
        return await _call_api(call, "add_known_uid")

    async def _remove_known_uid(call: ServiceCall):
        return await _call_api(call, "remove_known_uid")

    async def _add_unknown_uid(call: ServiceCall):
        return await _call_api(call, "add_unknown_uid")

    async def _remove_unknown_uid(call: ServiceCall):
        return await _call_api(call, "remove_unknown_uid")

    async def _run_shell_command(call: ServiceCall):
        return await _call_api(call, "run_shell_command")

    async def _list_known_uids(call: ServiceCall):
        return await _call_api(call, "list_known_uids")

    async def _list_unknown_uids(call: ServiceCall):
        return await _call_api(call, "list_unknown_uids")

    hass.services.async_register(
        DOMAIN,
        SERVICE_UNLOCK,
        _unlock,
        schema=base_schema,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RUN_KNOWN_SHELL_COMMAND,
        _run_known_shell_command,
        schema=base_schema,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_KNOWN_UID,
        _add_known_uid,
        schema=uid_schema,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_KNOWN_UID,
        _remove_known_uid,
        schema=uid_remove_schema,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_UNKNOWN_UID,
        _add_unknown_uid,
        schema=uid_remove_schema,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_UNKNOWN_UID,
        _remove_unknown_uid,
        schema=uid_remove_schema,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RUN_SHELL_COMMAND,
        _run_shell_command,
        schema=shell_schema,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_KNOWN_UIDS,
        _list_known_uids,
        schema=base_schema,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_UNKNOWN_UIDS,
        _list_unknown_uids,
        schema=base_schema,
        supports_response=SupportsResponse.ONLY,
    )
    hass.data[DOMAIN][DATA_REGISTERED] = True
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await async_setup(hass, {})

    api = AppleHomeKeyReaderApi(hass, entry.data)
    coordinator = AppleHomeKeyReaderCoordinator(hass, api)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        _LOGGER.debug(
            "Initial UID list fetch failed for entry %s; will retry on next poll",
            entry.entry_id,
        )

    entry_data = {
        "api": api,
        "coordinator": coordinator,
        "selection": {"new_uid": None, "known_uid": None},
    }
    hass.data[DOMAIN]["entries"][entry.entry_id] = entry_data
    hass.data[DOMAIN][DATA_APIS][entry.entry_id] = api

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        return False

    hass.data[DOMAIN]["entries"].pop(entry.entry_id, None)
    hass.data[DOMAIN][DATA_APIS].pop(entry.entry_id, None)

    if not hass.data[DOMAIN][DATA_APIS] and hass.data[DOMAIN].get(DATA_REGISTERED):
        for svc in (
            SERVICE_UNLOCK,
            SERVICE_RUN_KNOWN_SHELL_COMMAND,
            SERVICE_ADD_KNOWN_UID,
            SERVICE_REMOVE_KNOWN_UID,
            SERVICE_ADD_UNKNOWN_UID,
            SERVICE_REMOVE_UNKNOWN_UID,
            SERVICE_RUN_SHELL_COMMAND,
            SERVICE_LIST_KNOWN_UIDS,
            SERVICE_LIST_UNKNOWN_UIDS,
        ):
            hass.services.async_remove(DOMAIN, svc)
        hass.data[DOMAIN][DATA_REGISTERED] = False
    return True
