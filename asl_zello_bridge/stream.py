import io
import asyncio
import logging
import time


class AsyncByteStream:
    def __init__(self):
        self._buffer = io.BytesIO()
        self._data_available = asyncio.Event()
        self._lock = asyncio.Lock()
        self._logger = logging.getLogger('AsyncByteStream')

        # Diagnostic: timestamp of the previous write.
        self._last_write_mono = None

    async def write(self, data: bytes):
        # Diagnostic only: measure the time between PCM writes.
        now = time.monotonic()

        if self._last_write_mono is None:
            interval_ms = None
        else:
            interval_ms = (now - self._last_write_mono) * 1000.0

        self._last_write_mono = now

        async with self._lock:
            before = self._buffer.getbuffer().nbytes
            self._buffer.write(data)
            after = self._buffer.getbuffer().nbytes
            self._data_available.set()

            if self._logger.isEnabledFor(logging.DEBUG):
                if interval_ms is None:
                    interval_text = "first"
                else:
                    interval_text = f"{interval_ms:.1f}ms"

                self._logger.debug(
                    f"STREAM WRITE: added={len(data)}B "
                    f"buffer={before}B->{after}B "
                    f"interval={interval_text}"
                )

    async def has_data(self) -> bool:
        async with self._lock:
            size = self._buffer.getbuffer().nbytes
            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(
                    f"STREAM HAS_DATA: {size}B"
                )
            return size > 0

    async def read(self, n: int = -1) -> bytes:
        waited = False

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
                    if self._logger.isEnabledFor(logging.DEBUG):
                        self._logger.debug(
                            f"STREAM READ: requested={n}B "
                            f"returned={len(data)}B "
                            f"remaining={len(remaining_data)}B "
                            f"waited={waited}"
                        )
                    return data

            if not waited:
                if self._logger.isEnabledFor(logging.DEBUG):
                    self._logger.debug(
                        f"STREAM READ WAIT: buffer empty, requested={n}B"
                    )
                waited = True

            await self._data_available.wait()

            if waited and self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug("STREAM READ WAKE: data became available")