"""BJC Newsletter Home Assistant integration.

Polls the Boca Jewish Center website hourly for a new weekly newsletter,
extracts the schedule using Gemini AI, and exposes it as HA sensors.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN
from .coordinator import BJCNewsletterCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up BJC Newsletter from a config entry."""
    coordinator = BJCNewsletterCoordinator(hass, entry)

    # Run the first refresh. The coordinator pre-populates from the disk cache,
    # so if the BJC homepage is temporarily unreachable but we have cached data,
    # we allow setup to succeed rather than blocking HA startup.
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        cached_schedule = (coordinator.data or {}).get("schedule")
        if cached_schedule:
            _LOGGER.warning(
                "BJC newsletter first refresh failed but cached data is available: %s", err
            )
        else:
            raise ConfigEntryNotReady(
                f"Cannot reach BJC homepage and no cached schedule data: {err}"
            ) from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
