import asyncio
import logging
import math
import os
import socket
import struct

from .stream import AsyncByteStream

USRP_FRAME_SIZE = 352
USRP_HEADER_SIZE = 32
USRP_VOICE_SIZE = USRP_FRAME_SIZE - USRP_HEADER_SIZE

USRP_GAIN_RX_DB = int(os.environ.get('USRP_GAIN_RX_DB', 0))
USRP_GAIN_TX_DB = int(os.environ.get('USRP_GAIN_TX_DB', 0))

USRP_TYPE_VOICE = 0


def db_to_linear(db):
    return math.pow(10, db / 10)


def apply_gain(buf, gain):
    # USRP is PCM @ 8kHz, 16 bit signed. We should only ever get 20ms chunks,
    # but safer not to assume so.
    format = f'{int(len(buf) / 2)}h'
    pre_gain = struct.unpack(format, buf)
    return struct.pack(format, *[clamp_short(gain * b) for b in pre_gain])


def clamp_short(sh):
    return int(max(-32768, min(32767, sh)))


class USRPController(asyncio.DatagramProtocol):
    def __init__(self,
                 stream_in: AsyncByteStream,
                 stream_out: AsyncByteStream):

        self._stream_in = stream_in
        self._stream_out = stream_out

        self._tx_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._tx_seq = 0
        self._tx_seq_lock = asyncio.Lock()
        self._tx_address = os.environ.get('USRP_HOST')
        self._tx_port = int(os.environ.get('USRP_TXPORT', 7070))

        self._usrp_gain_rx = db_to_linear(USRP_GAIN_RX_DB)
        self._usrp_gain_tx = db_to_linear(USRP_GAIN_TX_DB)

        self._logger = logging.getLogger('USRPController')
        self._logger.info(f'USRP RX gain: {USRP_GAIN_RX_DB}dB = {self._usrp_gain_rx}')
        self._logger.info(f'USRP TX gain: {USRP_GAIN_TX_DB}dB = {self._usrp_gain_tx}')

    def connection_made(self, transport):
        pass

    def datagram_received(self, data, addr):
        loop = asyncio.get_running_loop()
        frame = data[USRP_HEADER_SIZE:]

        if self._usrp_gain_rx != 1:
            frame = apply_gain(frame, self._usrp_gain_rx)

        loop.create_task(self._stream_out.write(frame))

    async def run(self):
        # rx is handled by DatagramProtocol parent class
        await self.run_tx()

    def _tx_encode_state(self):
        return 'USRP'.encode('ascii') \
            + struct.pack('>iiiiiii',
                          self._tx_seq, 0,
                          True, 0,
                          USRP_TYPE_VOICE, 0, 0)

    async def run_tx(self):
        while True:
            pcm = await self._stream_in.read(USRP_VOICE_SIZE)
            if len(pcm) == 0:
                await asyncio.sleep(0)
            else:
                async with self._tx_seq_lock:
                    header = self._tx_encode_state()

                    if self._usrp_gain_tx != 1:
                        pcm = apply_gain(pcm, self._usrp_gain_tx)

                    frame = header + pcm
                    self._tx_socket.sendto(frame, (self._tx_address, self._tx_port))
                    self._tx_seq = self._tx_seq + 1
                    await asyncio.sleep(0)

