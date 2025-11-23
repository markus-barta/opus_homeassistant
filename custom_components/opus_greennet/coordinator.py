"""Data coordinator for Opus GreenNet Bridge integration."""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.mqtt import ReceiveMessage
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    DOMAIN,
    TOPIC_BASE,
    TOPIC_GET_DEVICES,
    TOPIC_PUT_STATE,
    TOPIC_SUB_DEVICE,
    TOPIC_SUB_GET_ANSWER,
    TOPIC_SUB_TELEGRAM_FROM,
)
from .enocean_device import EnOceanDevice

_LOGGER = logging.getLogger(__name__)

# Dispatcher signals
SIGNAL_DEVICE_DISCOVERED = f"{DOMAIN}_device_discovered"
SIGNAL_DEVICE_STATE_UPDATE = f"{DOMAIN}_device_state_update"


class OpusGreenNetCoordinator:
    """Coordinator for managing MQTT communication with Opus GreenNet Bridge."""

    def __init__(self, hass: HomeAssistant, eag_id: str) -> None:
        """Initialize the coordinator."""
        self.hass = hass
        self.eag_id = eag_id
        self.devices: dict[str, EnOceanDevice] = {}
        self._subscriptions: list[Callable[[], None]] = []
        self._discovery_complete = False

    async def async_setup(self) -> bool:
        """Set up the coordinator and start MQTT subscriptions."""
        _LOGGER.debug("Setting up Opus GreenNet coordinator for EAG %s", self.eag_id)

        # Subscribe to telegram stream (device state updates)
        topic_telegram = TOPIC_SUB_TELEGRAM_FROM.format(
            base=TOPIC_BASE, eag_id=self.eag_id
        )
        self._subscriptions.append(
            await mqtt.async_subscribe(
                self.hass, topic_telegram, self._handle_telegram_message, qos=1
            )
        )
        _LOGGER.debug("Subscribed to telegram topic: %s", topic_telegram)

        # Subscribe to device stream (device info updates)
        topic_device = TOPIC_SUB_DEVICE.format(base=TOPIC_BASE, eag_id=self.eag_id)
        self._subscriptions.append(
            await mqtt.async_subscribe(
                self.hass, topic_device, self._handle_device_message, qos=1
            )
        )
        _LOGGER.debug("Subscribed to device topic: %s", topic_device)

        # Subscribe to get answer (response to device queries)
        topic_get_answer = TOPIC_SUB_GET_ANSWER.format(
            base=TOPIC_BASE, eag_id=self.eag_id
        )
        self._subscriptions.append(
            await mqtt.async_subscribe(
                self.hass, topic_get_answer, self._handle_get_answer_message, qos=1
            )
        )
        _LOGGER.debug("Subscribed to get answer topic: %s", topic_get_answer)

        # Request all devices from the gateway
        await self._request_devices()

        return True

    async def async_unload(self) -> None:
        """Unload the coordinator and unsubscribe from MQTT."""
        _LOGGER.debug("Unloading Opus GreenNet coordinator for EAG %s", self.eag_id)
        for unsubscribe in self._subscriptions:
            unsubscribe()
        self._subscriptions.clear()

    async def _request_devices(self) -> None:
        """Request all devices from the gateway."""
        topic = TOPIC_GET_DEVICES.format(base=TOPIC_BASE, eag_id=self.eag_id)
        _LOGGER.debug("Requesting devices from gateway: %s", topic)
        await mqtt.async_publish(self.hass, topic, "", qos=1)

    @callback
    def _handle_telegram_message(self, msg: ReceiveMessage) -> None:
        """Handle incoming telegram messages (device state updates)."""
        try:
            payload = json.loads(msg.payload)
            telegram = payload.get("telegram", payload)

            device_id = telegram.get("deviceId")
            friendly_id = telegram.get("friendlyId")

            if not device_id:
                _LOGGER.warning("Received telegram without deviceId: %s", msg.topic)
                return

            _LOGGER.debug(
                "Received telegram from device %s (%s): %s",
                device_id,
                friendly_id,
                telegram.get("functions"),
            )

            # Get or create device
            device_key = friendly_id or device_id
            if device_key not in self.devices:
                # Create a basic device entry if we haven't discovered it yet
                self.devices[device_key] = EnOceanDevice(
                    device_id=device_id,
                    friendly_id=friendly_id or device_id,
                    physical_device=telegram.get("physicalDevice", ""),
                )
                _LOGGER.info(
                    "Auto-discovered device from telegram: %s (%s)",
                    friendly_id,
                    device_id,
                )
                async_dispatcher_send(
                    self.hass,
                    f"{SIGNAL_DEVICE_DISCOVERED}_{self.eag_id}",
                    self.devices[device_key],
                )

            # Update device state
            device = self.devices[device_key]
            device.update_from_telegram(telegram)

            # Notify listeners of state update
            async_dispatcher_send(
                self.hass,
                f"{SIGNAL_DEVICE_STATE_UPDATE}_{self.eag_id}_{device_key}",
                device,
            )

        except json.JSONDecodeError as err:
            _LOGGER.error("Failed to parse telegram message: %s", err)
        except Exception as err:
            _LOGGER.exception("Error handling telegram message: %s", err)

    @callback
    def _handle_device_message(self, msg: ReceiveMessage) -> None:
        """Handle incoming device messages (device info)."""
        try:
            payload = json.loads(msg.payload)
            device = EnOceanDevice.from_device_object(payload)

            if not device.device_id:
                _LOGGER.warning("Received device message without deviceId")
                return

            device_key = device.friendly_id or device.device_id
            is_new = device_key not in self.devices

            # Merge with existing device data if present
            if device_key in self.devices:
                existing = self.devices[device_key]
                device.channels = existing.channels  # Preserve channel state

            self.devices[device_key] = device

            _LOGGER.info(
                "Device %s: %s (%s) - EEPs: %s",
                "discovered" if is_new else "updated",
                device.friendly_id,
                device.device_id,
                [eep.get("eep") for eep in device.eeps],
            )

            if is_new:
                async_dispatcher_send(
                    self.hass,
                    f"{SIGNAL_DEVICE_DISCOVERED}_{self.eag_id}",
                    device,
                )

        except json.JSONDecodeError as err:
            _LOGGER.error("Failed to parse device message: %s", err)
        except Exception as err:
            _LOGGER.exception("Error handling device message: %s", err)

    @callback
    def _handle_get_answer_message(self, msg: ReceiveMessage) -> None:
        """Handle get answer messages (response to device queries)."""
        # Extract device identifier from topic
        # Topic format: EnOcean/{EAG-Identifier}/getAnswer/devices/{Device-Identifier}
        parts = msg.topic.split("/")
        if len(parts) >= 5:
            device_identifier = parts[-1]
            _LOGGER.debug("Received get answer for device: %s", device_identifier)

        # Reuse device message handler
        self._handle_device_message(msg)

    async def async_send_command(
        self,
        device_id: str,
        functions: list[dict[str, Any]],
    ) -> None:
        """Send a command to a device."""
        topic = TOPIC_PUT_STATE.format(
            base=TOPIC_BASE, eag_id=self.eag_id, device_id=device_id
        )

        # Build telegram object
        telegram = {
            "telegram": {
                "deviceId": device_id,
                "friendlyId": device_id,
                "direction": "to",
                "functions": functions,
            }
        }

        payload = json.dumps(telegram)
        _LOGGER.debug("Sending command to %s: %s", topic, payload)

        await mqtt.async_publish(self.hass, topic, payload, qos=1)

    async def async_turn_on(
        self,
        device_id: str,
        channel: int = 0,
        brightness: int | None = None,
    ) -> None:
        """Turn on a switch or light."""
        functions = [{"key": "channel", "value": str(channel)}]

        if brightness is not None:
            functions.append({"key": "dimValue", "value": str(brightness)})
        else:
            functions.append({"key": "switch", "value": "on"})

        await self.async_send_command(device_id, functions)

    async def async_turn_off(self, device_id: str, channel: int = 0) -> None:
        """Turn off a switch or light."""
        functions = [
            {"key": "channel", "value": str(channel)},
            {"key": "switch", "value": "off"},
        ]
        await self.async_send_command(device_id, functions)

    async def async_set_cover_position(
        self,
        device_id: str,
        position: int,
        channel: int = 0,
    ) -> None:
        """Set cover position (0 = closed, 100 = open)."""
        functions = [
            {"key": "channel", "value": str(channel)},
            {"key": "position", "value": str(position)},
        ]
        await self.async_send_command(device_id, functions)

    async def async_set_cover_tilt(
        self,
        device_id: str,
        tilt: int,
        channel: int = 0,
    ) -> None:
        """Set cover tilt angle."""
        functions = [
            {"key": "channel", "value": str(channel)},
            {"key": "angle", "value": str(tilt)},
        ]
        await self.async_send_command(device_id, functions)

    async def async_stop_cover(self, device_id: str, channel: int = 0) -> None:
        """Stop cover movement."""
        functions = [
            {"key": "channel", "value": str(channel)},
            {"key": "position", "value": "stop"},
        ]
        await self.async_send_command(device_id, functions)

    def get_device(self, device_id: str) -> EnOceanDevice | None:
        """Get a device by ID."""
        return self.devices.get(device_id)

    def get_devices_by_type(self, entity_type: str) -> list[EnOceanDevice]:
        """Get all devices of a specific entity type."""
        return [
            device
            for device in self.devices.values()
            if device.entity_type == entity_type
        ]
