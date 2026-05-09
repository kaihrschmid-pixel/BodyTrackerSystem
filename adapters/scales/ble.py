"""
adapters/scales/ble.py
======================
Generic Bluetooth Low Energy (BLE) scale adapter.

Supports any BLE scale that broadcasts weight measurements as GATT
notifications — covers the vast majority of consumer BLE scales:
  - Xiaomi Mi Body Composition Scale 2 (XMTZC05HM)
  - Renpho ES-CS20M
  - Yunmai, Picooc, and most rebranded variants

How BLE scales work:
  The scale broadcasts weight as GATT notifications on a well-known
  service UUID. We scan for the device by name or address, subscribe
  to the weight characteristic, and wait for a "stable" reading
  (most scales send a few unstable readings then a final stable one).

Requires: pip install bleak

Configuration:
    hardware:
      scale:
        adapter: "ble"
        device_name: "MI Scale"      # Bluetooth name (partial match ok)
        # OR
        device_address: "AA:BB:CC:DD:EE:FF"  # fixed MAC (more reliable)
        scan_timeout_s: 30
        stable_reading_count: 3      # consecutive identical readings = stable
        unit: "kg"
"""

from __future__ import annotations

import asyncio
import logging
import struct
from datetime import timezone
from typing import Optional

from adapters.base import ScaleAdapter, ScaleReading, utcnow

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Known BLE UUIDs
# Body Composition Service: 0x181B (Bluetooth SIG assigned)
# Weight Scale Service:     0x181D
# Custom Xiaomi service:    varies by firmware
# ------------------------------------------------------------------

BODY_COMPOSITION_SERVICE    = "0000181b-0000-1000-8000-00805f9b34fb"
WEIGHT_SCALE_SERVICE        = "0000181d-0000-1000-8000-00805f9b34fb"
BODY_COMPOSITION_FEATURE    = "00002a9b-0000-1000-8000-00805f9b34fb"
BODY_COMPOSITION_MEASUREMENT = "00002a9c-0000-1000-8000-00805f9b34fb"
WEIGHT_MEASUREMENT_CHAR     = "00002a9d-0000-1000-8000-00805f9b34fb"

# Xiaomi-specific (used by Mi Scale 2)
XIAOMI_SERVICE   = "0000181b-0000-1000-8000-00805f9b34fb"
XIAOMI_CHAR      = "00002a9c-0000-1000-8000-00805f9b34fb"


class BLEScaleAdapter(ScaleAdapter):
    """
    BLE scale adapter using the bleak library.

    Implements a protocol-agnostic approach:
    1. Scan for the device
    2. Connect and discover services
    3. Subscribe to all known weight/body-composition characteristics
    4. Parse whichever notification format the scale uses
    5. Wait for a stable reading (n consecutive matching weights)
    6. Return the ScaleReading and disconnect
    """

    ADAPTER_NAME = "ble"

    def __init__(self, config: dict):
        super().__init__(config)
        self._device_name: Optional[str]    = config.get("device_name")
        self._device_address: Optional[str] = config.get("device_address")
        self._scan_timeout: float           = config.get("scan_timeout_s", 30.0)
        self._stable_count: int             = config.get("stable_reading_count", 3)
        self._unit: str                     = config.get("unit", "kg")
        self._client = None
        self._reading_queue: asyncio.Queue  = asyncio.Queue()

    # ------------------------------------------------------------------
    # ScaleAdapter interface
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        try:
            from bleak import BleakScanner, BleakClient
        except ImportError:
            raise ImportError(
                "bleak is required for BLE scales. "
                "Install it: pip install bleak  (or: pip install body-tracker[ble])"
            )

        logger.info(
            "Scanning for BLE scale (name=%r, address=%r, timeout=%.0fs)…",
            self._device_name, self._device_address, self._scan_timeout,
        )

        device = await self._find_device(BleakScanner)
        if not device:
            raise RuntimeError(
                f"BLE scale not found within {self._scan_timeout}s. "
                "Make sure the scale is active (step on it briefly) and in range."
            )

        logger.info("Found device: %s  [%s]", device.name, device.address)
        self._client = BleakClient(device, timeout=15.0)
        await self._client.connect()
        logger.info("BLE connected to %s", device.address)

    async def read(self) -> ScaleReading:
        """
        Subscribe to weight notifications and wait for a stable reading.
        A reading is "stable" when N consecutive notifications agree within 0.1 kg.
        """
        if not self._client or not self._client.is_connected:
            raise RuntimeError("Not connected. Call connect() first.")

        subscribed = await self._subscribe_to_measurements()
        if not subscribed:
            raise RuntimeError(
                "No weight characteristic found on this scale. "
                "The device may use a proprietary protocol not yet supported."
            )

        logger.info("Waiting for stable weight reading (step on the scale)…")
        reading = await self._wait_for_stable()
        return reading

    async def disconnect(self) -> None:
        if self._client and self._client.is_connected:
            await self._client.disconnect()
            logger.debug("BLE disconnected")

    def is_available(self) -> bool:
        return bool(self._device_name or self._device_address)

    # ------------------------------------------------------------------
    # Private: device discovery
    # ------------------------------------------------------------------

    async def _find_device(self, BleakScanner):
        """Scan and return the matching device, or None."""

        if self._device_address:
            # Direct address lookup — fastest path
            device = await BleakScanner.find_device_by_address(
                self._device_address, timeout=self._scan_timeout
            )
            return device

        # Scan by name (partial match, case-insensitive)
        target_name = (self._device_name or "").lower()
        devices = await BleakScanner.discover(timeout=self._scan_timeout)
        for d in devices:
            if d.name and target_name in d.name.lower():
                return d
            # Also check advertised service UUIDs
            if hasattr(d, "metadata") and d.metadata:
                uuids = d.metadata.get("uuids", [])
                if any(u.lower() in (BODY_COMPOSITION_SERVICE, WEIGHT_SCALE_SERVICE) for u in uuids):
                    logger.info("Found scale by service UUID: %s [%s]", d.name, d.address)
                    return d
        return None

    # ------------------------------------------------------------------
    # Private: characteristic subscription
    # ------------------------------------------------------------------

    async def _subscribe_to_measurements(self) -> bool:
        """Try known characteristics in order. Return True if at least one worked."""
        candidates = [
            BODY_COMPOSITION_MEASUREMENT,
            WEIGHT_MEASUREMENT_CHAR,
            XIAOMI_CHAR,
        ]
        subscribed = False
        for char_uuid in candidates:
            try:
                await self._client.start_notify(char_uuid, self._on_notification)
                logger.debug("Subscribed to characteristic %s", char_uuid)
                subscribed = True
            except Exception:
                pass  # characteristic not present on this device
        return subscribed

    def _on_notification(self, sender: int, data: bytes) -> None:
        """Called by bleak on each BLE notification. Parse and enqueue."""
        parsed = self._parse_payload(data)
        if parsed:
            self._reading_queue.put_nowait(parsed)
            logger.debug("BLE notification: %.1f kg (stable=%s)", parsed["weight_kg"], parsed.get("stable"))

    # ------------------------------------------------------------------
    # Private: payload parsing
    # ------------------------------------------------------------------

    def _parse_payload(self, data: bytes) -> Optional[dict]:
        """
        Try multiple known payload formats.
        Returns a dict with at minimum {"weight_kg": float, "stable": bool}.
        """
        # Try Bluetooth SIG Body Composition Measurement (0x2A9C)
        # Format: flags(2) + body_fat(2) + ... (IEEE 11073 float or uint16)
        if len(data) >= 14:
            result = self._parse_body_composition(data)
            if result:
                return result

        # Try Weight Scale Measurement (0x2A9D)
        # Format: flags(1) + weight(2) [+ timestamp(7)] [+ user_id(1)] [+ bmi(2)] [+ height(2)]
        if len(data) >= 3:
            result = self._parse_weight_measurement(data)
            if result:
                return result

        # Try Xiaomi Mi Scale 2 proprietary format
        if len(data) == 13 or len(data) == 10:
            result = self._parse_xiaomi(data)
            if result:
                return result

        logger.debug("Unknown BLE payload (%d bytes): %s", len(data), data.hex())
        return None

    def _parse_body_composition(self, data: bytes) -> Optional[dict]:
        """Parse Bluetooth SIG Body Composition Measurement (0x2A9C)."""
        try:
            flags = struct.unpack_from("<H", data, 0)[0]
            # Bit 0: measurement units (0=SI/kg, 1=imperial)
            unit_imperial = bool(flags & 0x01)
            # Bit 1: time stamp present
            # Bit 2: user ID present
            # Bit 3: basal metabolism present
            # Bit 4: muscle percentage present
            # Bit 5: muscle mass present
            # Bit 6: fat free mass present
            # Bit 7: soft lean mass present
            # Bit 8: body water mass present
            # Bit 9: impedance present
            # Bit 10: weight present
            # Bit 11: height present

            offset = 2
            body_fat_pct = None
            weight_kg = None

            # Body fat percentage is always present at offset 2 in this format
            if len(data) > offset + 1:
                raw_fat = struct.unpack_from("<H", data, offset)[0]
                body_fat_pct = raw_fat * 0.1  # resolution 0.1%
                offset += 2

            # Weight (bit 10)
            if flags & (1 << 10):
                raw_weight = struct.unpack_from("<H", data, offset)[0]
                weight_kg = raw_weight * 0.005 if not unit_imperial else raw_weight * 0.01 * 0.453592
                offset += 2

            if weight_kg and 10.0 < weight_kg < 300.0:
                result = {"weight_kg": round(weight_kg, 1), "stable": True}
                if body_fat_pct and 0 < body_fat_pct < 80:
                    result["body_fat_pct"] = round(body_fat_pct, 1)
                return result
        except Exception:
            pass
        return None

    def _parse_weight_measurement(self, data: bytes) -> Optional[dict]:
        """Parse Bluetooth SIG Weight Scale Measurement (0x2A9D)."""
        try:
            flags = data[0]
            unit_imperial = bool(flags & 0x01)
            raw_weight = struct.unpack_from("<H", data, 1)[0]
            weight_kg = raw_weight * 0.005 if not unit_imperial else raw_weight * 0.01 * 0.453592
            if 10.0 < weight_kg < 300.0:
                return {"weight_kg": round(weight_kg, 1), "stable": True}
        except Exception:
            pass
        return None

    def _parse_xiaomi(self, data: bytes) -> Optional[dict]:
        """
        Parse Xiaomi Mi Body Composition Scale 2 proprietary format.
        13-byte payload: [ctrl][unit][year_hi][year_lo][month][day]
                         [hour][min][sec][impedance_hi][impedance_lo][weight_hi][weight_lo]
        Weight is in 100g units (divide by 200 for kg).
        Bit 5 of ctrl byte = measurement is stable.
        """
        try:
            if len(data) == 13:
                ctrl = data[0]
                is_stable = bool(ctrl & 0x20)
                raw_weight = struct.unpack_from(">H", data, 11)[0]
                weight_kg = raw_weight / 200.0
                if 10.0 < weight_kg < 300.0:
                    result = {"weight_kg": round(weight_kg, 1), "stable": is_stable}
                    # Impedance available for body composition calculation
                    # (requires age/height from profile for BIA formulas)
                    impedance = struct.unpack_from(">H", data, 9)[0]
                    if impedance > 0:
                        result["impedance_ohm"] = impedance
                    return result

            if len(data) == 10:
                # Simpler format (some firmware versions)
                raw_weight = struct.unpack_from(">H", data, 8)[0]
                weight_kg = raw_weight / 200.0
                if 10.0 < weight_kg < 300.0:
                    return {"weight_kg": round(weight_kg, 1), "stable": True}
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Private: stable reading detection
    # ------------------------------------------------------------------

    async def _wait_for_stable(self) -> ScaleReading:
        """
        Collect notifications until N consecutive readings agree within 0.1 kg.
        Times out after scan_timeout_s.
        """
        consecutive = 0
        last_weight: Optional[float] = None
        all_extras: dict = {}

        deadline = asyncio.get_event_loop().time() + self._scan_timeout

        while asyncio.get_event_loop().time() < deadline:
            try:
                parsed = await asyncio.wait_for(
                    self._reading_queue.get(), timeout=5.0
                )
            except asyncio.TimeoutError:
                logger.debug("No BLE notification in 5s — still waiting…")
                continue

            weight = parsed["weight_kg"]
            is_stable = parsed.get("stable", False)

            if last_weight is None or abs(weight - last_weight) > 0.15:
                consecutive = 1
                last_weight = weight
                logger.debug("Weight update: %.1f kg", weight)
            else:
                consecutive += 1
                logger.debug("Stable count: %d/%d (%.1f kg)", consecutive, self._stable_count, weight)

            # Collect any extras (impedance, body fat from device)
            for k, v in parsed.items():
                if k not in ("weight_kg", "stable"):
                    all_extras[k] = v

            if consecutive >= self._stable_count or is_stable:
                logger.info("Stable reading confirmed: %.1f kg", weight)
                return ScaleReading(
                    recorded_at=utcnow(),
                    weight_kg=weight,
                    body_fat_pct=all_extras.pop("body_fat_pct", None),
                    extras=all_extras,
                    adapter_name=self.ADAPTER_NAME,
                    raw_payload=parsed,
                )

        raise TimeoutError(
            f"BLE scale did not produce a stable reading within {self._scan_timeout}s. "
            "Make sure you are standing still on the scale."
        )
