import asyncio
import logging
import math
import os
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
                 stream_out: AsyncByteStream,
                 usrp_ptt: asyncio.Event,
                 zello_ptt: asyncio.Event):

        self._stream_in = stream_in
        self._stream_out = stream_out

        self._tx_seq = 0
        self._tx_seq_lock = asyncio.Lock()
        self._tx_address = os.environ.get('USRP_HOST')
        self._tx_port = int(os.environ.get('USRP_TXPORT', 7070))
        self._transport = None

        self._usrp_ptt = usrp_ptt
        self._zello_ptt = zello_ptt

        self._usrp_gain_rx = db_to_linear(USRP_GAIN_RX_DB)
        self._usrp_gain_tx = db_to_linear(USRP_GAIN_TX_DB)

        self._logger = logging.getLogger('USRPController')
        self._logger.info(f'USRP RX gain: {USRP_GAIN_RX_DB}dB = {self._usrp_gain_rx}')
        self._logger.info(f'USRP TX gain: {USRP_GAIN_TX_DB}dB = {self._usrp_gain_tx}')

    def connection_made(self, transport):
        self._transport = transport

    def datagram_received(self, data, addr):
        ptt = self._frame_ptt_state(data)
        if not ptt:
            self._usrp_ptt.clear()
            return

        self._usrp_ptt.set()

        loop = asyncio.get_running_loop()
        frame = data[USRP_HEADER_SIZE:]

        if self._usrp_gain_rx != 1:
            frame = apply_gain(frame, self._usrp_gain_rx)

        loop.create_task(self._stream_out.write(frame))

    async def run(self):
        # rx is handled by DatagramProtocol parent class
        await self.run_tx()

    async def _tx_encode_state(self, ptt=True):
        seq = await self._get_seq()
        return 'USRP'.encode('ascii') \
            + struct.pack('>iiiiiii',
                          seq, 0,
                          ptt, 0,
                          USRP_TYPE_VOICE, 0, 0)

    def _rx_decode_state(self, frame):
        header = frame[4:USRP_HEADER_SIZE]
        seq, mem, ptt, tg, type, mpx, res = struct.unpack('>iiiiiii', header)
        return (seq, mem, ptt, tg, type, mpx, res)

    def _frame_ptt_state(self, frame):
        state = self._rx_decode_state(frame)
        return state[2] == 1

    async def _get_seq(self):
        async with self._tx_seq_lock:
            self._tx_seq = self._tx_seq + 1
            return self._tx_seq

    async def _tx_frame(self, pcm):
        header = await self._tx_encode_state(ptt=True)

        if self._usrp_gain_tx != 1:
            pcm = apply_gain(pcm, self._usrp_gain_tx)

        frame = header + pcm
        self._tx(frame)

    async def _tx_off(self):
        frame = await self._tx_encode_state(ptt=False)
        self._tx(frame)

    def _tx(self, frame):
        if self._transport is not None:
            self._transport.sendto(frame, (self._tx_address, self._tx_port))

    async def run_tx(self):
        while True:

            # Send PTT off packet if Zello PTT is off
            if not self._zello_ptt.is_set():
                await self._tx_off()

            # Wait for Zello PTT
            await self._zello_ptt.wait()

            pcm = await self._stream_in.read(USRP_VOICE_SIZE)
            if len(pcm) > 0:
                await self._tx_frame(pcm)
