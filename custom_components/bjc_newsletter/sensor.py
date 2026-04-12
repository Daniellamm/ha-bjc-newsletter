"""Sensor entities for the BJC Newsletter integration."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DATA_LAST_CHECKED,
    DATA_LAST_ERROR,
    DATA_LAST_PROCESSED,
    DATA_NEWSLETTER_URL,
    DATA_SCHEDULE,
    DATA_STATUS,
    DOMAIN,
    NAME,
    SENSOR_STATUS,
    SENSOR_TODAY,
    SENSOR_TOMORROW,
    STATUS_IDLE,
)
from .coordinator import BJCNewsletterCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BJCNewsletterCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            BJCTodayScheduleSensor(coordinator, entry),
            BJCTomorrowScheduleSensor(coordinator, entry),
            BJCNewsletterStatusSensor(coordinator, entry),
        ]
    )


class BJCBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for BJC Newsletter sensors. All appear under one HA device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BJCNewsletterCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=NAME,
            manufacturer="Boca Jewish Center",
            model="Weekly Newsletter",
            configuration_url="https://www.bocajewishcenter.org/",
        )

    @property
    def available(self) -> bool:
        """Stay available as long as we have any cached data, even if the last poll failed."""
        return self.coordinator.data is not None

    @property
    def _data(self) -> dict[str, Any]:
        return self.coordinator.data or {}

    @property
    def _schedule(self) -> dict[str, str]:
        return self._data.get(DATA_SCHEDULE) or {}

    def _get_schedule_for_date(self, target: date) -> str:
        """Return markdown for a specific date, falling back to 'weekly'."""
        return self._schedule.get(target.isoformat()) or self._schedule.get("weekly") or ""


class BJCTodayScheduleSensor(BJCBaseSensor):
    """Sensor exposing today's schedule as a markdown attribute."""

    def __init__(self, coordinator: BJCNewsletterCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_TODAY}"
        self._attr_name = "Today's Schedule"
        self._attr_icon = "mdi:calendar-today"

    @property
    def native_value(self) -> str:
        """State is today's ISO date string."""
        return date.today().isoformat()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        today = date.today()
        return {
            "schedule": self._get_schedule_for_date(today),
            "date": today.isoformat(),
        }


class BJCTomorrowScheduleSensor(BJCBaseSensor):
    """Sensor exposing tomorrow's schedule as a markdown attribute."""

    def __init__(self, coordinator: BJCNewsletterCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_TOMORROW}"
        self._attr_name = "Tomorrow's Schedule"
        self._attr_icon = "mdi:calendar-arrow-right"

    @property
    def native_value(self) -> str:
        return (date.today() + timedelta(days=1)).isoformat()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        tomorrow = date.today() + timedelta(days=1)
        return {
            "schedule": self._get_schedule_for_date(tomorrow),
            "date": tomorrow.isoformat(),
        }


class BJCNewsletterStatusSensor(BJCBaseSensor):
    """Sensor reporting integration status and newsletter metadata."""

    def __init__(self, coordinator: BJCNewsletterCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_STATUS}"
        self._attr_name = "Newsletter Status"
        self._attr_icon = "mdi:newspaper"

    @property
    def native_value(self) -> str:
        return self._data.get(DATA_STATUS, STATUS_IDLE)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "newsletter_url": self._data.get(DATA_NEWSLETTER_URL, ""),
            "last_processed": self._data.get(DATA_LAST_PROCESSED, ""),
            "last_checked": self._data.get(DATA_LAST_CHECKED, ""),
            "last_error": self._data.get(DATA_LAST_ERROR, ""),
        }
