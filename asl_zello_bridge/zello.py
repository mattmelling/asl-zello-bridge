import aiohttp

import asyncio
import base64
import json
import logging
import os
import socket
import struct
import jwt
import sys

from datetime import datetime, timedelta, timezone

from pyogg.opus_decoder import OpusDecoder
from pyogg.opus_encoder import OpusEncoder

from .stream import AsyncByteStream


AUTH_TOKEN_EXPIRY = 3600
AUTH_TOKEN_EXPIRY_THRESHOLD = 600

def unix_time():
    return int((datetime.now(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds())

def socket_setup_keepalive(sock):
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)

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

    def get_seq(self):
        seq = self._seq
        self._seq = seq + 1
        return seq

    async def get_token(self):

        # Zello Free
        if 'ZELLO_PRIVATE_KEY' in os.environ:
            self._logger.info('Private key detected, getting Zello Free token')
            return self.get_token_free()

        # Zello Work
        if 'ZELLO_API_ENDPOINT' in os.environ:
            self._logger.info('Zello Work API endpoint configured, getting token from workspace')
            return await self.get_token_work()

    async def get_token_work(self):
        endpoint = os.environ.get('ZELLO_API_ENDPOINT')
        self._logger.info(f'Using endpoint {endpoint}')
        async with aiohttp.ClientSession() as session:
            async with session.post(f'{endpoint}/user/gettoken', data={
                'username': os.environ.get('ZELLO_USERNAME'),
                'password': os.environ.get('ZELLO_PASSWORD')
            }) as response:
                txt = await response.text()
                if response.status == 200:
                    self._logger.info('Got Zello Work token successfully!')
                    return json.loads(txt)['token']
                self._logger.info(f'Failed to get Zello Work token: {response.status} {txt}')
                return None

    def load_private_key(self):
        with open(os.environ['ZELLO_PRIVATE_KEY'], 'rb') as f:
            return f.read()

    def get_token_free(self):
        expiry = datetime.now() + timedelta(seconds=AUTH_TOKEN_EXPIRY)
        key = self.load_private_key()
        token = jwt.encode({
            'iss': os.environ.get('ZELLO_ISSUER', ''),
            'exp': int(expiry.timestamp())
        }, key, algorithm='RS256')
        self._token_expiry = expiry
        return token

    async def authenticate(self, ws):
        # https://github.com/zelloptt/zello-channel-api/blob/master/AUTH.md
        payload = {
            'command': 'logon',
            'seq': self.get_seq(),
            'username': os.environ.get('ZELLO_USERNAME'),
            'password': os.environ.get('ZELLO_PASSWORD'),
            'channels': [os.environ.get('ZELLO_CHANNEL')]
        }

        # Use refresh token if we have one
        if self._refresh_token is not None:
            self._logger.info('Authenticating with refresh token')
            payload['refresh_token'] = self._refresh_token
            self._refresh_token = None
        else:
            self._logger.info('Authenticating with new token')
            payload['auth_token'] = await self.get_token()

        json_payload = json.dumps(payload)
        self._logger.info(json_payload)
        await ws.send_str(json_payload)

    async def run(self):
        try:
            await asyncio.gather(*[
                self.run_rx(),
                self.monitor(),
                self.run_tx(),
                self.ping()
            ])
        except Exception as e:
            self._logger.error(e)

    async def ping(self):
        self._logger.info('Ping task starting')
        while True:
            if self._ws is not None:
                await self._ws.ping()
            await asyncio.sleep(5)

    async def monitor(self):
        self._logger.info('Monitor task starting')
        while True:
            if self._ws is None:
                await asyncio.sleep(1)
                continue

            if self._token_expiry is None:
                await asyncio.sleep(0)
                continue

            time_until_expiry =  self._token_expiry - datetime.now()
            if time_until_expiry.seconds <= AUTH_TOKEN_EXPIRY_THRESHOLD and not self._txing:
                self._logger.info('Access token will expire soon, reauthenticating')
                await self.authenticate(self._ws)

            await asyncio.sleep(1)

    async def start_tx(self):
        if self._ws is None or self._ws.closed:
            return

        self._txing = True
        header = base64.b64encode(struct.pack('<hbb', 8000, 1, 20)).decode('utf8')
        start_stream = json.dumps({
            'command': 'start_stream',
            'seq': self.get_seq(),
            'channel': os.environ.get('ZELLO_CHANNEL'),
            'type': 'audio',
            'codec': 'opus',
            'codec_header': header,

            # USRP is 8kHz 16 bit pcm, 320 byte frame size, therefore
            # duration = (1 / 8000) * (320 / 2) = 20ms
            'packet_duration': 20
        })
        self._logger.info(start_stream)
        await self._ws.send_str(start_stream)
        while self._stream_id is None:
            await asyncio.sleep(0)

    async def _end_tx(self):
        if self._ws is None or self._ws.closed:
            return
        stop_stream = json.dumps({
            'command': 'stop_stream',
            'seq': self.get_seq(),
            'channel': os.environ.get('ZELLO_CHANNEL'),
            'stream_id': self._stream_id
        })
        self._logger.info(stop_stream)
        await self._ws.send_str(stop_stream)
        self._txing = False

    async def run_tx(self):
        self._logger.debug('run_tx starting')
        encoder = OpusEncoder()
        encoder.set_application('voip')
        encoder.set_sampling_frequency(8000)
        encoder.set_channels(1)

        sending = False
        pcm = []

        ws_close_count = 0

        while True:
            await asyncio.sleep(0)

            if self._ws is None or self._ws.closed:
                await asyncio.sleep(1)
                self._logger.info('WS closed!')
                ws_close_count += 1
                if ws_close_count == 5:
                    sys.exit(-1)
                continue

            ws_close_count = 0

            try:

                # Stop sending if USRP PTT is clear
                if not self._usrp_ptt.is_set() and sending:
                    sending = False
                    await self._end_tx()

                # Wait for USRP PTT to key
                await self._usrp_ptt.wait()

                pcm = await asyncio.wait_for(self._stream_in.read(640), timeout=1)

                if len(pcm) == 0 or self._zello_ptt.is_set() or not self._logged_in:
                    continue

                if not sending:
                    await self.start_tx()

                sending = True
                opus = encoder.encode(pcm)
                frame = struct.pack('>bii', 1, self._stream_id, 0) + opus

                if self._ws is not None and not self._ws.closed:
                    await self._ws.send_bytes(frame)

            except asyncio.TimeoutError:
                pcm = []
                if sending:
                    await self._end_tx()
                    sending = False
                continue

    async def run_rx(self):
        self._logger.debug('run_rx starting')
        conn = aiohttp.TCPConnector(family=socket.AF_INET, ssl=False)
        decoder = None
        loop = asyncio.get_running_loop()

        is_channel_available = False
        is_authorized = False

        decoder = OpusDecoder()
        decoder.set_channels(1)
        decoder.set_sampling_frequency(8000)

        self._logger.info(f"Connecting to {os.environ.get('ZELLO_WS_ENDPOINT')}")
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.ws_connect(os.environ.get('ZELLO_WS_ENDPOINT'), autoping=False, heartbeat=True) as ws:
                sock = ws._response.connection.transport.get_extra_info('socket')
                if sock:
                    socket_setup_keepalive(sock)

                await asyncio.wait_for(self.authenticate(ws), 3)
                self._ws = ws
                async for msg in ws:

                    if ws.closed:
                        self._logger.warning('Websocket closed!')
                        self._ws = None
                        break

                    if msg.type == aiohttp.WSMsgType.PING:
                        self._logger.debug('PING from server')
                        await ws.pong()
                    elif msg.type == aiohttp.WSMsgType.PONG:
                        self._logger.debug('PONG from server')
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        self._logger.debug('ERROR from server')
                        await conn.close()
                        break

                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        self._logger.info(msg)
                        data = json.loads(msg.data)

                        if 'error' in data:
                            self._logger.error(data)
                            await asyncio.sleep(0)
                            break

                        if 'command' in data:

                            # Stream starting
                            if data['command'] == 'on_stream_start':
                                self._logger.info('on_stream_start')
                                self._zello_ptt.set()

                            # Stream stopped
                            elif data['command'] == 'on_stream_stop':
                                self._logger.info('on_stream_stop')
                                self._zello_ptt.clear()

                            # Channel status command
                            elif data['command'] == 'on_channel_status':
                                is_channel_available = True

                        if 'success' in data:
                            is_authorized = True

                            # Response to stream start
                            if 'stream_id' in data:
                                self._stream_id = data['stream_id']

                            # Response to auth
                            elif 'refresh_token' in data:
                                self._logger.info('Authentication successful!')
                                self._refresh_token = data['refresh_token']

                        if (not is_authorized) and (not is_channel_available):
                            self._logger.error('Authentication failed')
                            break
                        else:
                            self._logger.info('Logged in!')
                            self._logged_in = True

                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        self._logger.info(f'Data packet {len(msg.data)} bytes')
                        data = msg.data[9:]
                        pcm = decoder.decode(bytearray(data))
                        await self._stream_out.write(pcm)
                    else:
                        self._logger.warning(f'Unhandled message: {msg}')

                    await asyncio.sleep(0)

        # Prevent loop spam
        await asyncio.sleep(1)
        loop.create_task(self.run_rx())
