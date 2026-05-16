from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import AppleHomeKeyReaderApi, AppleHomeKeyReaderError
from .const import DOMAIN
from .coordinator import AppleHomeKeyReaderCoordinator

_LOGGER = logging.getLogger(__name__)

_NONE_OPTION = "(none)"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    api: AppleHomeKeyReaderApi = data["api"]
    coordinator: AppleHomeKeyReaderCoordinator = data["coordinator"]
    selection: dict = data["selection"]

    async_add_entities(
        [
            UnlockDoorButton(api, entry),
            PromoteNewUidButton(api, coordinator, entry, selection),
            RemoveNewUidButton(api, coordinator, entry, selection),
            RemoveKnownUidButton(api, coordinator, entry, selection),
        ]
    )


class _BaseButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="Apple Home Key Reader",
        )

    async def _call(self, coro):
        try:
            await coro
        except AppleHomeKeyReaderError as exc:
            raise HomeAssistantError(str(exc)) from exc


class UnlockDoorButton(_BaseButton):
    _attr_icon = "mdi:door-open"

    def __init__(self, api: AppleHomeKeyReaderApi, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._api = api
        self._attr_unique_id = f"{entry.entry_id}_unlock"
        self._attr_name = "Unlock Door"

    async def async_press(self) -> None:
        await self._call(self._api.unlock())


class _UidActionButton(_BaseButton):
    def __init__(
        self,
        api: AppleHomeKeyReaderApi,
        coordinator: AppleHomeKeyReaderCoordinator,
        entry: ConfigEntry,
        selection: dict,
    ) -> None:
        super().__init__(entry)
        self._api = api
        self._coordinator = coordinator
        self._selection = selection


class PromoteNewUidButton(_UidActionButton):
    _attr_icon = "mdi:nfc-variant-off"

    def __init__(self, api, coordinator, entry, selection):
        super().__init__(api, coordinator, entry, selection)
        self._attr_unique_id = f"{entry.entry_id}_promote_new_uid"
        self._attr_name = "Add New UID to Known"

    async def async_press(self) -> None:
        uid = self._selection.get("new_uid")
        if not uid or uid == _NONE_OPTION:
            _LOGGER.warning("No new UID selected; press ignored")
            return
        await self._call(self._api.add_known_uid(uid))
        await self._call(self._api.remove_unknown_uid(uid))
        self._selection["new_uid"] = None
        await self._coordinator.async_request_refresh()


class RemoveNewUidButton(_UidActionButton):
    _attr_icon = "mdi:nfc-off"

    def __init__(self, api, coordinator, entry, selection):
        super().__init__(api, coordinator, entry, selection)
        self._attr_unique_id = f"{entry.entry_id}_remove_new_uid"
        self._attr_name = "Remove New UID"

    async def async_press(self) -> None:
        uid = self._selection.get("new_uid")
        if not uid or uid == _NONE_OPTION:
            _LOGGER.warning("No new UID selected; press ignored")
            return
        await self._call(self._api.remove_unknown_uid(uid))
        self._selection["new_uid"] = None
        await self._coordinator.async_request_refresh()


class RemoveKnownUidButton(_UidActionButton):
    _attr_icon = "mdi:account-minus"

    def __init__(self, api, coordinator, entry, selection):
        super().__init__(api, coordinator, entry, selection)
        self._attr_unique_id = f"{entry.entry_id}_remove_known_uid"
        self._attr_name = "Remove Known UID"

    async def async_press(self) -> None:
        option = self._selection.get("known_uid")
        if not option or option == _NONE_OPTION:
            _LOGGER.warning("No known UID selected; press ignored")
            return
        # Parse raw UID from formatted option "Name (AABB)" or plain "AABB"
        if option.endswith(")") and "(" in option:
            uid = option.rsplit("(", 1)[-1][:-1]
        else:
            uid = option
        await self._call(self._api.remove_known_uid(uid))
        self._selection["known_uid"] = None
        await self._coordinator.async_request_refresh()
