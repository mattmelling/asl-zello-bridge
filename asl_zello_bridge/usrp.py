import asyncio
import os
import socket
import struct

from .stream import AsyncByteStream

USRP_FRAME_SIZE = 352
USRP_HEADER_SIZE = 32
USRP_VOICE_SIZE = USRP_FRAME_SIZE - USRP_HEADER_SIZE

USRP_TYPE_VOICE = 0

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

    def connection_made(self, transport):
        pass

    def datagram_received(self, data, addr):
        loop = asyncio.get_running_loop()
        frame = data[USRP_HEADER_SIZE:]
        loop.create_task(self._stream_out.write(frame))

    async def run(self):
        await asyncio.gather(*[
            self.run_tx()
            # rx is handled by DatagramProtocol parent class
        ])

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
                    frame = header + pcm

                    self._tx_socket.sendto(frame, (self._tx_address, self._tx_port))
                    self._tx_seq = self._tx_seq + 1
                    await asyncio.sleep(0)

