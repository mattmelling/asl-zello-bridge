import aiohttp
import asyncio
import base64
import json
import logging
import os
import socket
import struct

from pyogg.opus_decoder import OpusDecoder
from pyogg.opus_encoder import OpusEncoder

from .stream import AsyncByteStream


class ZelloController:

    def __init__(self,
                 stream_in: AsyncByteStream,
                 stream_out: AsyncByteStream):
        self._logger = logging.getLogger('ZelloController')
        self._stream_out = stream_out
        self._stream_in = stream_in
        self._stream_id = None
        self._seq = 0

    def get_seq(self):
        seq = self._seq
        self._seq = seq + 1
        return seq

    async def authenticate(self, ws):
        # https://github.com/zelloptt/zello-channel-api/blob/master/AUTH.md
        await ws.send_str(json.dumps({
            'command': 'logon',
            'seq': self.get_seq(),
            'auth_token': os.environ.get('ZELLO_TOKEN'),
            'username': os.environ.get('ZELLO_USERNAME'),
            'password': os.environ.get('ZELLO_PASSWORD'),
            'channel': os.environ.get('ZELLO_CHANNEL')
        }))

        is_authorized = False
        is_channel_available = False
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if 'refresh_token' in data:
                    is_authorized = True
                elif 'command' in data and 'status' in data and data['command'] == 'on_channel_status':
                    is_channel_available = data['status'] == 'online'
                if is_authorized and is_channel_available:
                    break

        if not is_authorized or not is_channel_available:
            raise NameError('Authentication failed')

    async def run(self):
        await asyncio.gather(*[
            self.run_rx()
        ])


    async def start_tx(self, ws):
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
        await ws.send_str(start_stream)
        while self._stream_id is None:
            await asyncio.sleep(0)

    async def end_tx(self, ws):
        stop_stream = json.dumps({
            'command': 'stop_stream',
            'seq': self.get_seq(),
            'channel': os.environ.get('ZELLO_CHANNEL'),
            'stream_id': self._stream_id
        })
        self._logger.info(stop_stream)
        await ws.send_str(stop_stream)

    async def run_tx(self, ws):
        encoder = OpusEncoder()
        encoder.set_application('voip')
        encoder.set_sampling_frequency(8000)
        encoder.set_channels(1)

        sending = False
        pcm = []

        while True:

            try:
                pcm = await asyncio.wait_for(self._stream_in.read(320), timeout=1)
            except asyncio.TimeoutError:
                pcm = []

            if len(pcm) == 0:
                if sending:
                    await self.end_tx(ws)
                    sending = False

            else:
                if not sending:
                    await self.start_tx(ws)
                    sending = True

                opus = encoder.encode(pcm)
                frame = struct.pack('>bii', 1, self._stream_id, 0) + opus
                await ws.send_bytes(frame)

    async def run_rx(self):
        self._logger.debug('run()')
        conn = aiohttp.TCPConnector(family=socket.AF_INET, ssl=False)
        decoder = None
        loop = asyncio.get_running_loop()

        async with aiohttp.ClientSession(connector = conn) as session:
            async with session.ws_connect(os.environ.get('ZELLO_ENDPOINT')) as ws:
                await asyncio.wait_for(self.authenticate(ws), 3)
                loop.create_task(self.run_tx(ws))
                async for msg in ws:
                    self._logger.info(msg)
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if 'command' in data:
                            if data['command'] == 'on_stream_start':
                                decoder = OpusDecoder()
                                decoder.set_channels(1)
                                decoder.set_sampling_frequency(8000)

                            elif data['command'] == 'on_stream_stop':
                                decoder = None
                        elif 'success' in data and 'stream_id' in data:
                            self._stream_id = data['stream_id']

                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        data = msg.data[9:]
                        pcm = decoder.decode(bytearray(data))
                        await self._stream_out.write(pcm)

                    await asyncio.sleep(0)
