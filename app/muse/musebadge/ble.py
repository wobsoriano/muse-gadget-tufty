"""BLE peripheral for Muse setup, on aioble. MicroPython only.

One GATT service with an RX characteristic the phone writes to and a TX
characteristic the badge notifies on. The UUIDs and the advertised name are
what the Muse app scans for.
"""

import asyncio

import aioble
import bluetooth

from .ble_framing import CHUNK_STAGGER_S, DEFAULT_ATT_MTU

SERVICE_UUID = bluetooth.UUID("7fdd3d1c-38ea-46cf-8b46-314ecf5f240c")
RX_UUID = bluetooth.UUID("4d593029-28a2-4a6e-a1f0-3c2d5e8f9b01")
TX_UUID = bluetooth.UUID("d75dc4ca-7b2b-4e9c-8f0a-1d2e3f4a5b6c")

# Manufacturer data the apps read as the device's paired flag. 0xFFFF is the
# unassigned company id; the value is informational, not authenticated.
PAIRED_FLAG_COMPANY_ID = 0xFFFF
ADV_INTERVAL_US = 100_000
# Phones write up to their negotiated MTU minus 3, and Android goes to 512.
MAX_WRITE_BYTES = 512
PREFERRED_MTU = 256


class BleServer:
    """Advertises as `name` and hands each write to `on_write(packet)`."""

    def __init__(self, name, on_write, on_connect, on_disconnect, log=print):
        self._name = name
        self._on_write = on_write
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._log = log
        self._connection = None
        self._registered = False

    def _register(self):
        if self._registered:
            return
        aioble.config(gap_name=self._name, mtu=PREFERRED_MTU)
        service = aioble.Service(SERVICE_UUID)
        self._rx = aioble.BufferedCharacteristic(
            service, RX_UUID, max_len=MAX_WRITE_BYTES,
            write=True, write_no_response=True, capture=True)
        self._tx = aioble.Characteristic(service, TX_UUID, read=True, notify=True)
        aioble.register_services(service)
        self._registered = True

    def mtu(self):
        connection = self._connection
        return (connection.mtu if connection else None) or DEFAULT_ATT_MTU

    async def send_packets(self, packets):
        for index, packet in enumerate(packets):
            connection = self._connection
            if connection is None or not connection.is_connected():
                return
            if index:
                await asyncio.sleep(CHUNK_STAGGER_S)
            self._tx.notify(connection, packet)

    def disconnect(self, delay):
        asyncio.create_task(self._disconnect_after(delay))

    async def _disconnect_after(self, delay):
        await asyncio.sleep(delay)
        connection = self._connection
        if connection is not None:
            try:
                await connection.disconnect()
            except Exception:
                pass

    async def _read_writes(self):
        while True:
            connection, data = await self._rx.written()
            if connection is self._connection:
                self._on_write(bytes(data))

    async def run(self):
        """Advertise and serve one client at a time until cancelled."""
        self._register()
        reader = asyncio.create_task(self._read_writes())
        try:
            while True:
                self._log("advertising as " + self._name)
                connection = await aioble.advertise(
                    ADV_INTERVAL_US, name=self._name, services=[SERVICE_UUID],
                    manufacturer=(PAIRED_FLAG_COMPANY_ID, b"\x00"))
                self._connection = connection
                self._log("BLE client connected")
                self._on_connect()
                try:
                    await connection.disconnected(timeout_ms=None)
                except Exception:
                    pass
                self._connection = None
                self._on_disconnect()
        finally:
            reader.cancel()
            connection = self._connection
            self._connection = None
            if connection is not None:
                try:
                    await connection.disconnect()
                except Exception:
                    pass
