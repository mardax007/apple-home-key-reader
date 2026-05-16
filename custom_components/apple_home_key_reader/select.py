from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

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
    coordinator: AppleHomeKeyReaderCoordinator = data["coordinator"]
    selection: dict = data["selection"]

    entities = [
        NewUidSelect(coordinator, entry, selection),
        KnownUidSelect(coordinator, entry, selection),
    ]
    # Store references so button.py can reach the same selection dict
    data["new_uid_select"] = entities[0]
    data["known_uid_select"] = entities[1]
    async_add_entities(entities)


class _BaseUidSelect(CoordinatorEntity, SelectEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AppleHomeKeyReaderCoordinator,
        entry: ConfigEntry,
        selection: dict,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._selection = selection

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="Apple Home Key Reader",
        )


class NewUidSelect(_BaseUidSelect):
    _attr_icon = "mdi:nfc-search-variant"

    def __init__(self, coordinator, entry, selection):
        super().__init__(coordinator, entry, selection)
        self._attr_unique_id = f"{entry.entry_id}_new_uid_select"
        self._attr_name = "New UID"

    @property
    def options(self) -> list[str]:
        uids = (self.coordinator.data or {}).get("new_uids", [])
        return list(uids) if uids else [_NONE_OPTION]

    @property
    def current_option(self) -> str | None:
        sel = self._selection.get("new_uid")
        if sel not in self.options:
            sel = self.options[0] if self.options else None
            self._selection["new_uid"] = sel
        return sel

    async def async_select_option(self, option: str) -> None:
        self._selection["new_uid"] = option
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        uids = (self.coordinator.data or {}).get("new_uids", [])
        return {"count": len(uids)}


class KnownUidSelect(_BaseUidSelect):
    _attr_icon = "mdi:nfc-tap"

    def __init__(self, coordinator, entry, selection):
        super().__init__(coordinator, entry, selection)
        self._attr_unique_id = f"{entry.entry_id}_known_uid_select"
        self._attr_name = "Known UID"

    def _uid_entries(self) -> list[dict]:
        return (self.coordinator.data or {}).get("known_uids", [])

    @property
    def options(self) -> list[str]:
        entries = self._uid_entries()
        if not entries:
            return [_NONE_OPTION]
        result = []
        for entry in entries:
            uid = entry["uid"] if isinstance(entry, dict) else str(entry)
            name = entry.get("name") if isinstance(entry, dict) else None
            result.append(f"{name} ({uid})" if name else uid)
        return result

    @property
    def current_option(self) -> str | None:
        sel = self._selection.get("known_uid")
        if sel not in self.options:
            sel = self.options[0] if self.options else None
            self._selection["known_uid"] = sel
        return sel

    async def async_select_option(self, option: str) -> None:
        self._selection["known_uid"] = option
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        entries = self._uid_entries()
        names = {
            e["uid"]: e.get("name")
            for e in entries
            if isinstance(e, dict) and e.get("name")
        }
        return {"count": len(entries), "uid_names": names}

    def extract_uid(self, option: str) -> str:
        """Parse raw UID from a formatted option string like 'Name (AABB)' or 'AABB'."""
        if option.endswith(")") and "(" in option:
            return option.rsplit("(", 1)[-1][:-1]
        return option
