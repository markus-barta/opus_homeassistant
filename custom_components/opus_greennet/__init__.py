"""The Opus GreenNet Bridge integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_EAG_ID, DOMAIN, PLATFORMS
from .coordinator import OpusGreenNetCoordinator

_LOGGER = logging.getLogger(__name__)

# Platforms to set up
PLATFORMS_LIST: list[Platform] = [Platform.LIGHT, Platform.SWITCH, Platform.COVER]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Opus GreenNet Bridge from a config entry."""
    eag_id = entry.data[CONF_EAG_ID]
    _LOGGER.info("Setting up Opus GreenNet Bridge: %s", eag_id)

    # Create coordinator
    coordinator = OpusGreenNetCoordinator(hass, eag_id)

    # Set up MQTT subscriptions
    try:
        if not await coordinator.async_setup():
            raise ConfigEntryNotReady("Failed to set up MQTT subscriptions")
    except Exception as err:
        _LOGGER.error("Failed to set up coordinator: %s", err)
        raise ConfigEntryNotReady from err

    # Store coordinator in hass.data
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS_LIST)

    # Register update listener for options
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    _LOGGER.info("Opus GreenNet Bridge setup complete: %s", eag_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading Opus GreenNet Bridge: %s", entry.data[CONF_EAG_ID])

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS_LIST)

    if unload_ok:
        # Clean up coordinator
        coordinator: OpusGreenNetCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_unload()

    return unload_ok


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)
