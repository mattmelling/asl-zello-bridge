"""
Zello <-> USRP bridge: Zello-side controller

This module manages the WebSocket connection to Zello Channels and
encodes/decodes audio frames to bridge with the USRP side.

Key improvements vs. original:
- Use aiohttp heartbeat (autoping=True, heartbeat=30) instead of a custom ping loop.
- Tuned OS TCP keepalive to reduce false-positive disconnects on quiet links.
- Rich diagnostics: always log WebSocket close codes and exceptions.
- "Logged in!" logs ONCE on state transition (auth success + channel status).
- TX path is gated on a connection-ready event to avoid misleading startup warnings.
- Clear docstrings, constants, and type hints for maintainability.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import socket
import struct
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import jwt
from pyogg.opus_decoder import OpusDecoder
from pyogg.opus_encoder import OpusEncoder

from .stream import AsyncByteStream

# -----------------------------------------------------------------------------
# Constants and defaults
# -----------------------------------------------------------------------------

# Zello auth token lifetime (seconds) and refresh threshold (seconds before expiry)
AUTH_TOKEN_EXPIRY: int = 3600
AUTH_TOKEN_EXPIRY_THRESHOLD: int = 600

# WebSocket heartbeat: aiohttp will send a ping at this interval (seconds)
WS_HEARTBEAT_SECS: int = 30

# Kernel TCP keepalive tuning (seconds)
TCP_KEEPIDLE: int = 120   # idle time before starting keepalive probes
TCP_KEEPINTVL: int = 30   # interval between keepalive probes
TCP_KEEPCNT: int = 5      # number of failed probes before declaring the connection dead

# USRP audio format (8 kHz, mono, 16-bit PCM)
USRP_SAMPLE_RATE: int = 8000
USRP_CHANNELS: int = 1
USRP_FRAME_MS: int = 20
# Size of one PCM frame from USRP (bytes): 20ms * 8000 samples/sec * 2 bytes/sample
USRP_PCM_BYTES: int = int(USRP_FRAME_MS / 1000 * USRP_SAMPLE_RATE * 2)  # 320 bytes


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def unix_time() -> int:
    """Return current Unix time (UTC) as an integer."""
    return int(
        (datetime.now(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()
    )


def socket_setup_keepalive(sock: socket.socket) -> None:
    """
    Enable and tune kernel TCP keepalive so dead connections are detected,
    but not so aggressively that quiet links get torn down.
    """
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, TCP_KEEPIDLE)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, TCP_KEEPINTVL)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, TCP_KEEPCNT)


# -----------------------------------------------------------------------------
# Zello Controller
# -----------------------------------------------------------------------------

class ZelloController:
    """
    Manages Zello Channels WebSocket session and audio bridging.

    Responsibilities:
    - Establish and monitor a WS connection to ZELLO_WS_ENDPOINT
    - Authenticate using a JWT (Zello Free) and refresh token flow
    - Start/stop outbound streams (USRP -> Zello) on PTT
    - Decode inbound Zello frames (Zello -> USRP) and forward as PCM
    """

    def __init__(
        self,
        stream_in: AsyncByteStream,
        stream_out: AsyncByteStream,
        usrp_ptt: asyncio.Event,
        zello_ptt: asyncio.Event,
    ) -> None:
        """
        Args:
            stream_in:  Async byte stream providing PCM frames from USRP (radio -> Zello)
            stream_out: Async byte stream to send PCM frames to USRP (Zello -> radio)
            usrp_ptt:   Event set when USRP side wants to transmit (key down)
            zello_ptt:  Event set when Zello side is transmitting (remote PTT)
        """
        self._logger = logging.getLogger("ZelloController")

        # Audio streams
        self._stream_out = stream_out
        self._stream_in = stream_in

        # Zello session state
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._stream_id: Optional[int] = None
        self._seq: int = 0
        self._logged_in: bool = False
        self._txing: bool = False

        # Auth state
        self._token_expiry: Optional[datetime] = None
        self._refresh_token: Optional[str] = None

        # PTT coordination
        self._usrp_ptt = usrp_ptt
        self._zello_ptt = zello_ptt

        # Connection readiness flag for TX to wait on
        self._ws_ready: asyncio.Event = asyncio.Event()

    # -------------------------------------------------------------------------
    # Auth helpers
    # -------------------------------------------------------------------------

    def get_seq(self) -> int:
        """Return an incrementing sequence number for Zello commands."""
        seq = self._seq
        self._seq = seq + 1
        return seq

    async def get_token(self) -> Optional[str]:
        """
        Construct a JWT for Zello Free if a private key is configured.

        Returns:
            The JWT string, or None if not applicable (e.g., Zello Work flow).
        """
        if "ZELLO_PRIVATE_KEY" in os.environ:
            self._logger.info("Private key detected, generating Zello Free token")
            return self._get_token_free()
        return None

    def _load_private_key(self) -> bytes:
        """Load the RS256 private key from ZELLO_PRIVATE_KEY."""
        with open(os.environ["ZELLO_PRIVATE_KEY"], "rb") as f:
            return f.read()

    def _get_token_free(self) -> str:
        """Create a Zello Free JWT with RS256 signature."""
        expiry = datetime.now() + timedelta(seconds=AUTH_TOKEN_EXPIRY)
        key = self._load_private_key()
        token = jwt.encode(
            {
                "iss": os.environ.get("ZELLO_ISSUER", ""),
                "exp": int(expiry.timestamp()),
            },
            key,
            algorithm="RS256",
        )
        self._token_expiry = expiry
        return token

    async def authenticate(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """
        Authenticate to Zello with either a refresh token or a new JWT.

        Follows https://github.com/zelloptt/zello-channel-api/blob/master/AUTH.md
        """
        payload: dict = {
            "command": "logon",
            "seq": self.get_seq(),
            "username": os.environ.get("ZELLO_USERNAME"),
            "password": os.environ.get("ZELLO_PASSWORD"),
            "channels": [os.environ.get("ZELLO_CHANNEL")],
        }

        # Prefer refresh token if we have one (server granted from previous auth)
        if self._refresh_token is not None:
            self._logger.info("Authenticating with refresh token")
            payload["refresh_token"] = self._refresh_token
            self._refresh_token = None
        else:
            self._logger.info("Authenticating with new token (JWT)")
            payload["auth_token"] = await self.get_token()

        json_payload = json.dumps(payload)
        self._logger.info(f"Sending auth payload: {json_payload}")
        await ws.send_str(json_payload)

    # -------------------------------------------------------------------------
    # Lifecycle tasks
    # -------------------------------------------------------------------------

    async def run(self) -> None:
        """
        Main entrypoint: run RX (WebSocket), monitor (token refresh), and TX (USRP->Zello).
        """
        try:
            await asyncio.gather(
                self.run_rx(),
                self.monitor(),
                self.run_tx(),
            )
        except Exception as e:
            self._logger.error(
                f"Fatal error in ZelloController.run: {e}", exc_info=True
            )

    async def monitor(self) -> None:
        """
        Periodically refresh Zello auth before expiry (when not actively TXing).
        """
        self._logger.info("Monitor task starting")
        while True:
            # Not connected yet
            if self._ws is None:
                await asyncio.sleep(1)
                continue

            # No token in play (e.g., Zello Work flow) — nothing to refresh
            if self._token_expiry is None:
                await asyncio.sleep(0.5)
                continue

            time_until_expiry = self._token_expiry - datetime.now()
            if time_until_expiry.seconds <= AUTH_TOKEN_EXPIRY_THRESHOLD and not self._txing:
                self._logger.info("Access token expiring soon, reauthenticating")
                try:
                    await self.authenticate(self._ws)
                except Exception as e:
                    self._logger.error(f"Error refreshing token: {e}")

            await asyncio.sleep(1)

    # -------------------------------------------------------------------------
    # Transmit path (USRP -> Zello)
    # -------------------------------------------------------------------------

    async def start_tx(self) -> None:
        """Send Zello 'start_stream' command and wait for a stream_id."""
        if self._ws is None or self._ws.closed:
            return

        self._txing = True
        # Zello Opus header: little-endian <hbb: sample_rate, channels, frame_ms
        header = base64.b64encode(
            struct.pack("<hbb", USRP_SAMPLE_RATE, USRP_CHANNELS, USRP_FRAME_MS)
        ).decode("utf8")

        start_stream = json.dumps(
            {
                "command": "start_stream",
                "seq": self.get_seq(),
                "channel": os.environ.get("ZELLO_CHANNEL"),
                "type": "audio",
                "codec": "opus",
                "codec_header": header,
                # USRP is 8 kHz 16-bit mono, 320-byte frame => 20 ms packets
                "packet_duration": USRP_FRAME_MS,
            }
        )

        self._logger.info(f"Starting TX: {start_stream}")
        await self._ws.send_str(start_stream)

        # Wait for 'stream_id' in a server response (handled in RX loop)
        while self._stream_id is None:
            await asyncio.sleep(0)

    async def _end_tx(self) -> None:
        """Send Zello 'stop_stream' for the active stream."""
        if self._ws is None or self._ws.closed:
            return
        stop_stream = json.dumps(
            {
                "command": "stop_stream",
                "seq": self.get_seq(),
                "channel": os.environ.get("ZELLO_CHANNEL"),
                "stream_id": self._stream_id,
            }
        )
        self._logger.info(f"Stopping TX: {stop_stream}")
        await self._ws.send_str(stop_stream)
        self._txing = False

    async def run_tx(self) -> None:
        """
        Loop that watches USRP PTT and sends encoded Opus frames to Zello
        while PTT is asserted and we are logged in.

        TX now waits for a connection-ready event to avoid misleading
        "WS closed in run_tx loop" warnings during initial startup.
        """
        self._logger.debug("run_tx starting")

        encoder = OpusEncoder()
        encoder.set_application("voip")
        encoder.set_sampling_frequency(USRP_SAMPLE_RATE)
        encoder.set_channels(USRP_CHANNELS)

        sending = False
        ws_close_count = 0
        ever_connected = False  # becomes True after the first ready websocket

        while True:
            await asyncio.sleep(0)

            # Gate on connection readiness to suppress startup warnings
            if not self._ws_ready.is_set() or self._ws is None or self._ws.closed:
                await asyncio.sleep(1)
                if ever_connected:
                    self._logger.warning("WS closed in run_tx loop")
                continue

            ever_connected = True
            ws_close_count = 0

            try:
                # If USRP PTT released while sending, stop the stream
                if not self._usrp_ptt.is_set() and sending:
                    sending = False
                    await self._end_tx()

                # Wait for USRP PTT (key down)
                await self._usrp_ptt.wait()

                # Read one PCM frame (20ms)
                pcm = await asyncio.wait_for(self._stream_in.read(USRP_PCM_BYTES), timeout=1)

                # Skip if no audio, Zello is transmitting, or not logged in yet
                if len(pcm) == 0 or self._zello_ptt.is_set() or not self._logged_in:
                    continue

                if not sending:
                    await self.start_tx()

                sending = True

                # Encode PCM -> Opus and wrap in Zello media frame
                opus = encoder.encode(pcm)
                frame = struct.pack(">bii", 1, self._stream_id, 0) + opus

                if self._ws is not None and not self._ws.closed:
                    await self._ws.send_bytes(frame)

            except asyncio.TimeoutError:
                # If we had been sending but timed out on PCM, stop the stream
                if sending:
                    await self._end_tx()
                    sending = False
                continue

    # -------------------------------------------------------------------------
    # Receive path (Zello -> USRP)
    # -------------------------------------------------------------------------

    async def run_rx(self) -> None:
        """
        Establish and manage the Zello WebSocket; handle auth/messages and
        forward inbound Opus frames as PCM to the USRP side.
        """
        self._logger.debug("run_rx starting")

        # NOTE: ssl=False allows system OpenSSL defaults via TLS tunnel termination;
        # set to True if you need aiohttp to manage TLS verification explicitly.
        conn = aiohttp.TCPConnector(family=socket.AF_INET, ssl=False)
        loop = asyncio.get_running_loop()

        is_channel_available = False
        is_authorized = False

        # Prepare Opus decoder for inbound audio
        decoder = OpusDecoder()
        decoder.set_channels(USRP_CHANNELS)
        decoder.set_sampling_frequency(USRP_SAMPLE_RATE)

        ws_endpoint = os.environ.get("ZELLO_WS_ENDPOINT")
        self._logger.info(f"Connecting to {ws_endpoint}")

        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.ws_connect(
                ws_endpoint,
                autoping=True,              # aiohttp replies to PINGs automatically
                heartbeat=WS_HEARTBEAT_SECS # aiohttp sends client PINGs at this interval
            ) as ws:
                try:
                    self._logger.info(
                        f"Connected to {ws_endpoint} with heartbeat={WS_HEARTBEAT_SECS}s"
                    )

                    # Enable kernel TCP keepalive on the underlying socket
                    sock: socket.socket | None = ws._response.connection.transport.get_extra_info("socket")
                    if sock:
                        socket_setup_keepalive(sock)

                    # Send initial auth and expose ws to other tasks
                    await asyncio.wait_for(self.authenticate(ws), timeout=3)
                    self._ws = ws
                    self._ws_ready.set()  # <-- mark ready for TX

                    # Main receive loop
                    async for msg in ws:

                        # If the WS closed during iteration, capture details and break
                        if ws.closed:
                            self._logger.warning(
                                f"WebSocket closed mid-loop: code={ws.close_code}, exception={ws.exception()}"
                            )
                            self._logged_in = False
                            self._ws = None
                            self._ws_ready.clear()
                            break

                        # Control frames & errors
                        if msg.type == aiohttp.WSMsgType.PING:
                            self._logger.debug("PING from server")
                        elif msg.type == aiohttp.WSMsgType.PONG:
                            self._logger.debug("PONG from server")
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            self._logger.error(f"WebSocket error: {ws.exception()}")
                            await conn.close()
                            break

                        # Text frames: auth, control, channel status, etc.
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            self._logger.info(f"WS TEXT: {msg.data}")
                            data = json.loads(msg.data)

                            # Server reported error (auth/command)
                            if "error" in data:
                                self._logger.error(f"Auth/command error: {data}")
                                break

                            # Commands from server
                            if "command" in data:
                                cmd = data["command"]
                                if cmd == "on_stream_start":
                                    self._logger.info("on_stream_start")
                                    self._zello_ptt.set()
                                elif cmd == "on_stream_stop":
                                    self._logger.info("on_stream_stop")
                                    self._zello_ptt.clear()
                                elif cmd == "on_channel_status":
                                    is_channel_available = True

                            # Success responses
                            if "success" in data:
                                is_authorized = True
                                # Start_stream response contains a stream_id
                                if "stream_id" in data:
                                    self._stream_id = data["stream_id"]
                                # Auth response contains a refresh_token
                                elif "refresh_token" in data:
                                    self._logger.info("Authentication successful!")
                                    self._refresh_token = data["refresh_token"]

                            # ---- Login transition logic (fix repeated "Logged in!") ----
                            # Flip to logged_in only once when both are true.
                            if (not self._logged_in) and is_authorized and is_channel_available:
                                self._logged_in = True
                                self._logger.info("Logged in!")

                        # Binary frames: encoded Opus audio from Zello
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            self._logger.info(f"Data packet {len(msg.data)} bytes")
                            # Zello media frame header is 9 bytes; payload thereafter is Opus
                            opus_payload = msg.data[9:]
                            pcm = decoder.decode(bytearray(opus_payload))
                            await self._stream_out.write(pcm)

                        else:
                            # Any newly-introduced frame types will show up here
                            self._logger.warning(f"Unhandled WS message: {msg}")

                        await asyncio.sleep(0)

                finally:
                    # This always runs, even on exceptions, providing close diagnostics
                    self._logger.warning(
                        f"WebSocket finally closed: code={ws.close_code}, exception={ws.exception()}"
                    )
                    # Ensure state is reset if we somehow reach here without the mid-loop close
                    self._logged_in = False
                    self._ws = None
                    self._ws_ready.clear()

        # Prevent tight reconnect loops; schedule a new run_rx task
        await asyncio.sleep(1)
        loop.create_task(self.run_rx())
