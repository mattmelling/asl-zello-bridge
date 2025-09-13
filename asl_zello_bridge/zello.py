import aiohttp

import asyncio
import base64
import json
import logging
import os
import socket
import struct
import jwt
import time

from datetime import datetime, timedelta, timezone

from pyogg.opus_decoder import OpusDecoder
from pyogg.opus_encoder import OpusEncoder

from .stream import AsyncByteStream


AUTH_TOKEN_EXPIRY = 3600
AUTH_TOKEN_EXPIRY_THRESHOLD = 600

POST_LOGIN_COOLDOWN_SEC = 0.8
CHANNEL_NOT_READY_BACKOFF_SEC = 0.5
AUTH_WATCHDOG_SEC = 8.0

PTT_IDLE_SLEEP_SEC = 0.003
MAIN_LOOP_YIELD_SEC = 0.001


def socket_setup_keepalive(sock):
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass


class ZelloController:

    def __init__(self,
                 stream_in: AsyncByteStream,
                 stream_out: AsyncByteStream,
                 usrp_ptt: asyncio.Event,
                 zello_ptt: asyncio.Event):
        self._logger = logging.getLogger('ZelloController')
        self._stream_out = stream_out
        self._stream_in = stream_in
        self._stream_id = None
        self._seq = 0

        self._token_expiry = None
        self._refresh_token = None
        self._logged_in = False

        self._usrp_ptt = usrp_ptt
        self._zello_ptt = zello_ptt

        self._ws = None
        self._txing = False

        self._auth_lock = asyncio.Lock()

        self._talk_user = None
        self._talk_start = None

        self._usrp_tx_start = None

        self._tasks = []
        self._shutdown = False

        self._pkt_id = 0
        self._private_key = None
        self._woodpecker_until = None
        self._empty_msg_backoff_until = None
        self._ptt_down_at = None

        self._in_woodpecker_backoff = False
        self._in_empty_backoff = False

        self._last_skip_reason = None
        self._last_skip_reason_at = None

        self._frame_window_start = None
        self._frame_count = 0
        self._frame_bytes = 0

        self._channel_ready = False
        self._auth_in_progress = False
        self._last_login_at = None
        self._channel_backoff_until = None
        self._start_retry_after = None
        self._auth_started_at = None
        self._auth_seq = None

        self._stat_start_attempts = 0
        self._stat_start_ok = 0
        self._stat_channel_not_ready = 0
        self._stat_read_timeouts = 0

        self._codec_header_b64 = base64.b64encode(
            struct.pack('<hbb', 8000, 1, 20)).decode('utf8')

    def get_seq(self):
        seq = self._seq
        self._seq = seq + 1
        return seq

    async def get_token(self):
        if 'ZELLO_PRIVATE_KEY' in os.environ:
            self._logger.info('Private key detected, getting Zello Free token')
            return self.get_token_free()
        return None

    def load_private_key(self):
        if self._private_key is None:
            with open(os.environ['ZELLO_PRIVATE_KEY'], 'rb') as f:
                self._private_key = f.read()
        return self._private_key

    def get_token_free(self):
        expiry = datetime.now(timezone.utc) + \
            timedelta(seconds=AUTH_TOKEN_EXPIRY)
        key = self.load_private_key()
        token = jwt.encode({
            'iss': os.environ.get('ZELLO_ISSUER', ''),
            'exp': int(expiry.timestamp())
        }, key, algorithm='RS256')
        self._token_expiry = expiry
        return token

    def _redact(self, obj):
        try:
            data = dict(obj)
        except Exception:
            return obj
        for k in ('password', 'auth_token', 'refresh_token'):
            if k in data and data[k] is not None:
                val = str(data[k])
                if k == 'password':
                    data[k] = '<redacted>'
                else:
                    data[k] = val[:12] + '…<redacted>'
        return data

    def _debug_skip(self, reason):
        now = datetime.now(timezone.utc)
        if self._last_skip_reason != reason or self._last_skip_reason_at is None or (now - self._last_skip_reason_at).total_seconds() >= 2:
            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(f"TX skip: {reason}")
            self._last_skip_reason = reason
            self._last_skip_reason_at = now

    def _frame_summary_maybe_emit(self):
        if not self._txing:
            self._frame_window_start = None
            self._frame_count = 0
            self._frame_bytes = 0
            return
        if self._frame_window_start is None:
            self._frame_window_start = time.monotonic()
            return
        elapsed = time.monotonic() - self._frame_window_start
        if elapsed >= 1.0 and self._frame_count > 0:
            if self._logger.isEnabledFor(logging.DEBUG):
                pps = self._frame_count / elapsed
                avg = self._frame_bytes / self._frame_count
                kb = self._frame_bytes / 1024.0
                self._logger.debug(
                    f"TX summary: stream_id={self._stream_id} frames={self._frame_count} avg_size={avg:.1f}B pps={pps:.1f} bytes={kb:.2f}KB")
            self._frame_window_start = time.monotonic()
            self._frame_count = 0
            self._frame_bytes = 0

    async def authenticate(self, ws):
        payload = {
            'command': 'logon',
            'seq': self.get_seq(),
            'username': os.environ.get('ZELLO_USERNAME'),
            'password': os.environ.get('ZELLO_PASSWORD'),
            'channels': [os.environ.get('ZELLO_CHANNEL')]
        }
        self._auth_in_progress = True
        self._auth_started_at = time.monotonic()
        self._auth_seq = payload['seq']
        used_refresh = False
        if self._refresh_token is not None:
            self._logger.info('Authenticating with refresh token')
            payload['refresh_token'] = self._refresh_token
            self._refresh_token = None
            used_refresh = True
        else:
            self._logger.info('Authenticating with new token')
            payload['auth_token'] = await self.get_token()
        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(
                f"Sending logon payload: {json.dumps(self._redact(payload))}")
            self._logger.debug(
                "Auth method: refresh_token" if used_refresh else "Auth method: auth_token")
        self._logger.info('Logging in...')
        await ws.send_str(json.dumps(payload))

    def _reset_connection_state(self):
        self._ws = None
        self._logged_in = False
        self._stream_id = None
        self._txing = False
        self._talk_user = None
        self._talk_start = None
        self._usrp_tx_start = None
        self._channel_ready = False
        self._auth_in_progress = False
        self._channel_backoff_until = None
        self._start_retry_after = None
        self._auth_started_at = None
        self._auth_seq = None
        if self._zello_ptt.is_set():
            self._zello_ptt.clear()

    async def shutdown(self):
        self._shutdown = True
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._logger.info(
            f"Stats: start_attempts={self._stat_start_attempts} start_ok={self._stat_start_ok} "
            f"chn_not_ready={self._stat_channel_not_ready} read_timeouts={self._stat_read_timeouts}"
        )

    async def run(self):
        try:
            self._tasks = [
                asyncio.create_task(self.run_rx()),
                asyncio.create_task(self.monitor()),
                asyncio.create_task(self.run_tx())
            ]
            await asyncio.gather(*self._tasks)
        except Exception as e:
            self._logger.error(f"Run error: {e}")
            await self.shutdown()

    async def monitor(self):
        self._logger.info('Monitor task starting')
        try:
            while not self._shutdown:
                now_dt = datetime.now(timezone.utc)
                if self._ws is None:
                    await asyncio.sleep(1)
                    continue
                if self._token_expiry is None:
                    await asyncio.sleep(1)
                    # auth watchdog even if token unknown
                # auth watchdog
                if self._auth_in_progress and self._auth_started_at is not None:
                    if (time.monotonic() - self._auth_started_at) > AUTH_WATCHDOG_SEC:
                        self._logger.warning(
                            "Auth watchdog tripped; clearing auth_in_progress")
                        self._auth_in_progress = False
                        self._auth_started_at = None
                        self._auth_seq = None
                if self._token_expiry is not None:
                    time_until_expiry = self._token_expiry - now_dt
                    if time_until_expiry.total_seconds() <= AUTH_TOKEN_EXPIRY_THRESHOLD and not self._txing:
                        if self._logger.isEnabledFor(logging.DEBUG):
                            self._logger.debug(
                                f"Token expires in {time_until_expiry.total_seconds():.0f}s, reauthenticating")
                        self._logger.info(
                            'Access token will expire soon, reauthenticating')
                        async with self._auth_lock:
                            if self._ws is not None and not self._ws.closed:
                                try:
                                    await self.authenticate(self._ws)
                                except Exception as e:
                                    self._logger.error(
                                        f'Reauthentication failed: {e}')
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug('Monitor task cancelled')
        except Exception as e:
            self._logger.error(f'Monitor task error: {e}')

    async def start_tx(self):
        if self._ws is None or self._ws.closed:
            return
        now = datetime.now(timezone.utc)
        if self._auth_in_progress:
            self._debug_skip("auth in progress")
            return
        if not self._channel_ready:
            if self._channel_backoff_until and now < self._channel_backoff_until:
                remaining = (self._channel_backoff_until - now).total_seconds()
                self._debug_skip(f"channel not ready ({remaining:.2f}s left)")
            else:
                self._debug_skip("channel not ready")
            return
        if self._last_login_at and (now - self._last_login_at).total_seconds() < POST_LOGIN_COOLDOWN_SEC:
            remaining = POST_LOGIN_COOLDOWN_SEC - \
                (now - self._last_login_at).total_seconds()
            self._debug_skip(
                f"post-login cooldown ({max(0.0, remaining):.2f}s left)")
            return
        if self._start_retry_after and now < self._start_retry_after:
            remaining = (self._start_retry_after - now).total_seconds()
            self._debug_skip(f"waiting retry window ({remaining:.2f}s left)")
            return

        self._stat_start_attempts += 1
        self._stream_id = None
        self._usrp_tx_start = now
        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug("Requesting Zello stream...")
        seq_val = self.get_seq()
        start_payload = {
            'command': 'start_stream',
            'seq': seq_val,
            'channel': os.environ.get('ZELLO_CHANNEL'),
            'type': 'audio',
            'codec': 'opus',
            'codec_header': self._codec_header_b64,
            'packet_duration': 20
        }
        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(json.dumps(self._redact(start_payload)))
        try:
            await self._ws.send_str(json.dumps(start_payload))
            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(
                    f"Waiting for stream_id assignment (seq={seq_val})...")
            timeout = 2.0
            interval = 0.05
            waited = 0.0
            while self._stream_id is None and waited < timeout:
                await asyncio.sleep(interval)
                waited += interval
            if self._stream_id is None:
                self._logger.error('Failed to get stream_id within timeout')
                if self._logger.isEnabledFor(logging.DEBUG):
                    self._logger.debug("Timed out waiting for stream_id")
                self._start_retry_after = datetime.now(
                    timezone.utc) + timedelta(seconds=CHANNEL_NOT_READY_BACKOFF_SEC)
                return
            self._txing = True
            self._pkt_id = 0
            self._frame_window_start = None
            self._frame_count = 0
            self._frame_bytes = 0
            self._start_retry_after = None
            self._stat_start_ok += 1
            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(
                    f"Started Zello stream {self._stream_id} (seq={seq_val})")
        except Exception as e:
            self._logger.error(f'Failed to start stream: {e}')

    async def _end_tx(self):
        if self._ws is None or self._ws.closed:
            self._txing = False
            return
        if self._stream_id is None:
            self._logger.warning('Ending TX but no stream_id available')
            self._txing = False
            return
        seq_val = self.get_seq()
        stop_payload = {
            'command': 'stop_stream',
            'seq': seq_val,
            'channel': os.environ.get('ZELLO_CHANNEL'),
            'stream_id': self._stream_id
        }
        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug(
                f"Requesting stop for Zello stream {self._stream_id} (seq={seq_val})")
            self._logger.debug(json.dumps(self._redact(stop_payload)))
        sid = self._stream_id
        try:
            await self._ws.send_str(json.dumps(stop_payload))
            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(f"Stopped Zello stream {sid}")
        except Exception as e:
            self._logger.error(f'Failed to send stop_stream: {e}')
        self._usrp_tx_start = None
        self._txing = False

    async def run_tx(self):
        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug('run_tx starting')
        encoder = OpusEncoder()
        encoder.set_application('voip')
        encoder.set_sampling_frequency(8000)
        encoder.set_channels(1)
        try:
            comp = int(os.getenv('OPUS_COMPLEXITY', '5'))
            try:
                encoder.set_complexity(comp)
            except Exception:
                pass
            br_env = os.getenv('OPUS_BITRATE', '')
            if br_env:
                try:
                    encoder.set_bitrate(int(br_env))
                except Exception:
                    pass
        except Exception:
            pass
        sending = False
        first_pcm_logged = False
        pcm = []
        try:
            while not self._shutdown:
                await asyncio.sleep(MAIN_LOOP_YIELD_SEC)
                now = datetime.now(timezone.utc)
                # auth watchdog here too for extra safety
                if self._auth_in_progress and self._auth_started_at is not None:
                    if (time.monotonic() - self._auth_started_at) > AUTH_WATCHDOG_SEC:
                        self._logger.warning(
                            "Auth watchdog (TX loop) tripped; clearing auth_in_progress")
                        self._auth_in_progress = False
                        self._auth_started_at = None
                        self._auth_seq = None

                if self._ws is None or self._ws.closed:
                    await asyncio.sleep(0.5)
                    continue
                if self._woodpecker_until and now < self._woodpecker_until:
                    if not self._in_woodpecker_backoff and self._logger.isEnabledFor(logging.DEBUG):
                        self._logger.debug(
                            f"Woodpecker backoff until {self._woodpecker_until}")
                        self._in_woodpecker_backoff = True
                    await asyncio.sleep(0.05)
                    continue
                if self._in_woodpecker_backoff and (not self._woodpecker_until or now >= self._woodpecker_until):
                    if self._logger.isEnabledFor(logging.DEBUG):
                        self._logger.debug(
                            "Woodpecker backoff ended, resuming TX attempts")
                    self._in_woodpecker_backoff = False
                if self._empty_msg_backoff_until and now < self._empty_msg_backoff_until:
                    if not self._in_empty_backoff and self._logger.isEnabledFor(logging.DEBUG):
                        self._logger.debug(
                            f"Empty-message backoff until {self._empty_msg_backoff_until}")
                        self._in_empty_backoff = True
                    await asyncio.sleep(0.05)
                    continue
                if self._in_empty_backoff and (not self._empty_msg_backoff_until or now >= self._empty_msg_backoff_until):
                    if self._logger.isEnabledFor(logging.DEBUG):
                        self._logger.debug(
                            "Empty-message backoff ended, resuming TX attempts")
                    self._in_empty_backoff = False

                try:
                    if not self._usrp_ptt.is_set():
                        if self._ptt_down_at:
                            dur = (now - self._ptt_down_at).total_seconds()
                            self._logger.info(f'UnKeyed:USRP ({dur:.1f}s)')
                            if self._logger.isEnabledFor(logging.DEBUG):
                                self._logger.debug(
                                    f"USRP PTT released at {now}")
                            self._ptt_down_at = None
                        if sending:
                            sending = False
                            first_pcm_logged = False
                            await self._end_tx()
                        await asyncio.sleep(PTT_IDLE_SLEEP_SEC)
                        continue
                    else:
                        if self._ptt_down_at is None:
                            self._ptt_down_at = now
                            self._logger.info('Keyed:USRP')
                            if self._logger.isEnabledFor(logging.DEBUG):
                                self._logger.debug(
                                    f"USRP PTT down at {self._ptt_down_at}")
                        if (datetime.now(timezone.utc) - self._ptt_down_at).total_seconds() < 0.15:
                            await asyncio.sleep(0.01)
                            continue

                    try:
                        pcm = await asyncio.wait_for(self._stream_in.read(640), timeout=1)
                    except asyncio.TimeoutError:
                        self._stat_read_timeouts += 1
                        self._debug_skip("stream_in read timeout")
                        pcm = []
                        if sending:
                            await self._end_tx()
                            sending = False
                            first_pcm_logged = False
                        continue
                    if not pcm:
                        self._debug_skip("empty PCM")
                        continue
                    if len(pcm) < 320:
                        self._debug_skip("short PCM (<320)")
                        continue
                    if self._zello_ptt.is_set():
                        self._debug_skip("RX in progress (zello_ptt set)")
                        continue
                    if not self._logged_in:
                        self._debug_skip("not logged in")
                        continue
                    if self._auth_in_progress:
                        self._debug_skip("auth in progress")
                        continue
                    if not self._channel_ready:
                        if self._channel_backoff_until and now < self._channel_backoff_until:
                            remaining = (
                                self._channel_backoff_until - now).total_seconds()
                            self._debug_skip(
                                f"channel not ready ({remaining:.2f}s left)")
                        else:
                            self._debug_skip("channel not ready")
                        continue
                    if self._last_login_at and (now - self._last_login_at).total_seconds() < POST_LOGIN_COOLDOWN_SEC:
                        remaining = POST_LOGIN_COOLDOWN_SEC - \
                            (now - self._last_login_at).total_seconds()
                        self._debug_skip(
                            f"post-login cooldown ({max(0.0, remaining):.2f}s left)")
                        continue
                    if self._start_retry_after and now < self._start_retry_after:
                        remaining = (self._start_retry_after -
                                     now).total_seconds()
                        self._debug_skip(
                            f"waiting retry window ({remaining:.2f}s left)")
                        continue

                    if not first_pcm_logged and self._ptt_down_at:
                        if self._logger.isEnabledFor(logging.DEBUG):
                            latency_ms = (datetime.now(
                                timezone.utc) - self._ptt_down_at).total_seconds() * 1000.0
                            self._logger.debug(
                                f"First PCM after PTT: {latency_ms:.1f} ms (len={len(pcm)})")
                        first_pcm_logged = True

                    if not sending:
                        test_pcm = pcm
                        if not test_pcm or len(test_pcm) < 320:
                            self._debug_skip("prebuffer short")
                            continue
                        await self.start_tx()
                        if not self._txing or self._stream_id is None:
                            self._debug_skip("start_tx failed or no stream_id")
                            continue

                    opus = encoder.encode(pcm)
                    sending = True
                    if self._stream_id is not None and isinstance(self._stream_id, int):
                        frame = struct.pack(
                            '>bii', 1, self._stream_id, self._pkt_id) + opus
                        self._pkt_id = (self._pkt_id + 1) & 0x7FFFFFFF
                        if self._ws is not None and not self._ws.closed:
                            try:
                                await self._ws.send_bytes(frame)
                                self._frame_count += 1
                                self._frame_bytes += len(opus)
                                self._frame_summary_maybe_emit()
                            except Exception as e:
                                self._logger.error(
                                    f'Failed to send audio frame: {e}')
                                sending = False
                                first_pcm_logged = False
                                await self._end_tx()
                    else:
                        self._logger.warning(
                            f'Invalid stream_id: {self._stream_id}, stopping transmission')
                        sending = False
                        first_pcm_logged = False
                        await self._end_tx()
                except asyncio.TimeoutError:
                    pcm = []
                    if sending:
                        await self._end_tx()
                        sending = False
                        first_pcm_logged = False
                    continue
        except asyncio.CancelledError:
            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug('TX task cancelled')
            if sending:
                await self._end_tx()
        except Exception as e:
            self._logger.error(f'TX task error: {e}')

    async def run_rx(self):
        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug('run_rx starting')
        while not self._shutdown:
            try:
                conn = aiohttp.TCPConnector(family=socket.AF_INET, ssl=False)
                decoder = None
                is_channel_available = False
                is_authorized = False
                decoder = OpusDecoder()
                decoder.set_channels(1)
                decoder.set_sampling_frequency(8000)
                self._logger.info(
                    f"Connecting to {os.environ.get('ZELLO_WS_ENDPOINT')}")
                if self._logger.isEnabledFor(logging.DEBUG):
                    self._logger.debug(
                        f"Opening WebSocket to {os.environ.get('ZELLO_WS_ENDPOINT')}")
                async with aiohttp.ClientSession(connector=conn) as session:
                    try:
                        async with session.ws_connect(os.environ.get('ZELLO_WS_ENDPOINT'), autoping=True, heartbeat=30.0) as ws:
                            if self._logger.isEnabledFor(logging.DEBUG):
                                self._logger.debug(
                                    "WebSocket connection established")
                            sock = ws._response.connection.transport.get_extra_info(
                                'socket')
                            if sock:
                                socket_setup_keepalive(sock)
                            async with self._auth_lock:
                                await asyncio.wait_for(self.authenticate(ws), 10)
                            self._ws = ws
                            start_time = time.monotonic()
                            async for msg in ws:
                                if self._shutdown:
                                    break
                                if ws.closed:
                                    code = getattr(ws, 'close_code', None)
                                    reason = getattr(ws, 'exception', None)
                                    uptime = time.monotonic() - start_time
                                    self._logger.warning('Websocket closed!')
                                    if self._logger.isEnabledFor(logging.DEBUG):
                                        self._logger.debug(
                                            f"WebSocket connection closed code={code} uptime={uptime:.1f}s reason={reason}")
                                        self._logger.debug(
                                            f"Session summary: tx_active={self._txing} channel_ready={self._channel_ready} "
                                            f"woodpecker_until={self._woodpecker_until} empty_until={self._empty_msg_backoff_until}"
                                        )
                                    # ensure auth flag is cleared if socket closes mid-auth
                                    self._auth_in_progress = False
                                    self._auth_started_at = None
                                    self._auth_seq = None
                                    break
                                if msg.type == aiohttp.WSMsgType.ERROR:
                                    self._logger.error(
                                        f'WebSocket error: {msg.data}')
                                    break
                                elif msg.type == aiohttp.WSMsgType.TEXT:
                                    try:
                                        data = json.loads(msg.data)
                                        if self._logger.isEnabledFor(logging.DEBUG):
                                            self._logger.debug(
                                                f"RX TEXT: {json.dumps(self._redact(data))}")
                                    except json.JSONDecodeError as e:
                                        self._logger.error(
                                            f'Failed to parse JSON: {e}')
                                        continue

                                    if 'error' in data:
                                        if data.get('error') == 'kicked':
                                            self._logger.error(
                                                f'Kicked from channel: {self._redact(data)}')
                                            self._reset_connection_state()
                                            break
                                        elif data.get('error') == 'woodpecker prohibited':
                                            self._logger.warning(
                                                f'Woodpecker protection triggered: {self._redact(data)}')
                                            backoff = 3
                                            if self._woodpecker_until and datetime.now(timezone.utc) < self._woodpecker_until:
                                                prev = (
                                                    self._woodpecker_until - datetime.now(timezone.utc)).seconds
                                                backoff = min(8, prev * 2)
                                            self._woodpecker_until = datetime.now(
                                                timezone.utc) + timedelta(seconds=backoff)
                                            if self._logger.isEnabledFor(logging.DEBUG):
                                                self._logger.debug(
                                                    f"Applying woodpecker backoff until {self._woodpecker_until}")
                                            if self._txing:
                                                await self._end_tx()
                                            continue
                                        elif data.get('error') == 'empty message':
                                            self._logger.warning(
                                                f'Server error: {self._redact(data)}')
                                            if self._empty_msg_backoff_until and datetime.now(timezone.utc) < self._empty_msg_backoff_until:
                                                prev = (
                                                    self._empty_msg_backoff_until - datetime.now(timezone.utc)).seconds
                                                backoff = min(8, prev * 2)
                                            else:
                                                backoff = 1
                                            self._empty_msg_backoff_until = datetime.now(
                                                timezone.utc) + timedelta(seconds=backoff)
                                            if self._logger.isEnabledFor(logging.DEBUG):
                                                self._logger.debug(
                                                    f"Applying empty-message backoff until {self._empty_msg_backoff_until}")
                                            if self._txing:
                                                await self._end_tx()
                                            continue
                                        elif data.get('error') == 'channel is not ready':
                                            seq = data.get('seq')
                                            self._logger.warning(
                                                f"Channel not ready (seq={seq})")
                                            self._stat_channel_not_ready += 1
                                            self._channel_ready = False
                                            self._channel_backoff_until = datetime.now(
                                                timezone.utc) + timedelta(seconds=CHANNEL_NOT_READY_BACKOFF_SEC)
                                            self._start_retry_after = self._channel_backoff_until
                                            if self._logger.isEnabledFor(logging.DEBUG):
                                                self._logger.debug(
                                                    f"Channel backoff until {self._channel_backoff_until}")
                                            continue
                                        else:
                                            self._logger.error(
                                                f'Server error: {self._redact(data)}')
                                            break

                                    if 'command' in data:
                                        if data['command'] == 'on_stream_start':
                                            user = data.get('from') or data.get('user') or data.get(
                                                'username') or data.get('display_name')
                                            self._talk_user = user
                                            self._talk_start = datetime.now(
                                                timezone.utc)
                                            if self._logger.isEnabledFor(logging.DEBUG):
                                                self._logger.debug(
                                                    f"Talk user set: {self._talk_user}")
                                            if user:
                                                self._logger.info(
                                                    f'Keyed:{user}')
                                            else:
                                                self._logger.info(
                                                    'Keyed:Unknown')
                                            self._zello_ptt.set()
                                        elif data['command'] == 'on_stream_stop':
                                            now_dt = datetime.now(timezone.utc)
                                            dur = None
                                            if self._talk_start is not None:
                                                dur = (
                                                    now_dt - self._talk_start).total_seconds()
                                            user = data.get('from') or data.get('user') or data.get(
                                                'username') or data.get('display_name') or self._talk_user
                                            if user is not None and dur is not None:
                                                self._logger.info(
                                                    f'UnKeyed:{user} ({dur:.1f}s)')
                                            elif user is not None:
                                                self._logger.info(
                                                    f'UnKeyed:{user}')
                                            else:
                                                self._logger.info(
                                                    'UnKeyed:Unknown')
                                            self._zello_ptt.clear()
                                            self._talk_user = None
                                            self._talk_start = None
                                        elif data['command'] == 'on_channel_status':
                                            status = data.get('status')
                                            desired = (status == 'online')
                                            if self._channel_ready != desired and self._logger.isEnabledFor(logging.DEBUG):
                                                self._logger.debug(
                                                    f"Channel ready -> {desired} (status='{status}')")
                                            self._channel_ready = desired
                                            if desired:
                                                self._logger.info(
                                                    "Channel is ready")

                                    if 'success' in data:
                                        is_authorized = True
                                        # Any success matching the auth seq clears auth_in_progress
                                        if self._auth_seq is not None and data.get('seq') == self._auth_seq:
                                            self._auth_in_progress = False
                                            self._auth_started_at = None
                                            self._auth_seq = None
                                            self._last_login_at = datetime.now(
                                                timezone.utc)
                                            self._start_retry_after = None
                                            if self._logger.isEnabledFor(logging.DEBUG):
                                                self._logger.debug(
                                                    "Authentication success event received (seq match)")
                                            # token info may come alongside or not
                                        if 'stream_id' in data:
                                            self._stream_id = data['stream_id']
                                            if self._logger.isEnabledFor(logging.DEBUG):
                                                self._logger.debug(
                                                    f"Received Zello stream_id: {self._stream_id} (seq={data.get('seq')})")
                                        if 'refresh_token' in data:
                                            self._logger.info(
                                                'Authentication successful!')
                                            self._refresh_token = data['refresh_token']
                                            if self._auth_started_at is not None and self._logger.isEnabledFor(logging.DEBUG):
                                                auth_ms = (
                                                    time.monotonic() - self._auth_started_at) * 1000.0
                                                self._logger.debug(
                                                    f"Auth completed in ~{auth_ms:.0f} ms")
                                            self._auth_in_progress = False
                                            self._auth_started_at = None
                                            self._auth_seq = None
                                            self._last_login_at = datetime.now(
                                                timezone.utc)
                                            self._start_retry_after = None
                                            try:
                                                exp = jwt.decode(self._refresh_token, options={
                                                                 "verify_signature": False}).get('exp')
                                                if exp:
                                                    self._token_expiry = datetime.fromtimestamp(
                                                        exp, tz=timezone.utc)
                                                    if self._logger.isEnabledFor(logging.DEBUG):
                                                        self._logger.debug(
                                                            f"Refresh token expiry set to {self._token_expiry}")
                                            except Exception as e:
                                                if self._logger.isEnabledFor(logging.DEBUG):
                                                    self._logger.debug(
                                                        f'Failed to decode refresh token expiry: {e}')

                                    if (not is_authorized) and (not is_channel_available):
                                        self._logger.error(
                                            'Authentication failed')
                                        break
                                    else:
                                        if not self._logged_in:
                                            self._logger.info('Logged in!')
                                        self._logged_in = True

                                elif msg.type == aiohttp.WSMsgType.BINARY:
                                    if self._logger.isEnabledFor(logging.DEBUG):
                                        self._logger.debug(
                                            f"RX BINARY {len(msg.data)} bytes")
                                    try:
                                        data = msg.data[9:]
                                        pcm = decoder.decode(bytearray(data))
                                        await self._stream_out.write(pcm)
                                    except Exception as e:
                                        self._logger.error(
                                            f'Failed to decode audio: {e} bytes={len(msg.data)}')
                                else:
                                    self._logger.warning(
                                        f'Unhandled message: {msg}')
                    except (aiohttp.ClientConnectorError, aiohttp.ClientConnectorDNSError) as e:
                        self._logger.warning(f'Connection error: {e}')
                        if self._logger.isEnabledFor(logging.DEBUG):
                            self._logger.debug(
                                "Network hint: DNS/connection issue; will retry in 5s")
                    except asyncio.TimeoutError:
                        self._logger.warning('Authentication timeout')
                    except Exception as e:
                        self._logger.error(f'WebSocket error: {e}')
                    finally:
                        self._reset_connection_state()
            except Exception as e:
                self._logger.error(f'RX task error: {e}')
            if not self._shutdown:
                await asyncio.sleep(5)
        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug('RX task exiting')
