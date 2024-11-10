import io
import asyncio


class AsyncByteStream:
    def __init__(self):
        self._buffer = io.BytesIO()
        self._data_available = asyncio.Event()
        self._lock = asyncio.Lock()

    async def write(self, data: bytes):
        async with self._lock:
            self._buffer.write(data)
            self._data_available.set()

    async def read(self, n: int = -1) -> bytes:
        while True:
            async with self._lock:
                self._buffer.seek(0)
                data = self._buffer.read(n)
                remaining_data = self._buffer.read()
                self._buffer = io.BytesIO()

                if len(remaining_data) > 0:
                    self._buffer.write(remaining_data)
                else:
                    self._data_available.clear()

                if data:
                    return data

            await self._data_available.wait()
