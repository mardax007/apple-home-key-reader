from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AppleHomeKeyReaderApi, AppleHomeKeyReaderError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(seconds=30)


class AppleHomeKeyReaderCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, api: AppleHomeKeyReaderApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.api = api

    async def _async_update_data(self) -> dict:
        try:
            known = await self.api.list_known_uids()
            new = await self.api.list_unknown_uids()
            return {
                "known_uids": known.get("uids", []) if isinstance(known, dict) else [],
                "new_uids": new.get("uids", []) if isinstance(new, dict) else [],
            }
        except AppleHomeKeyReaderError as exc:
            raise UpdateFailed(str(exc)) from exc
