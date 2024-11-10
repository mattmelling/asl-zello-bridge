import asyncio
import logging
import os

from .zello import ZelloController
from .usrp import USRPController
from .stream import AsyncByteStream

logging.basicConfig(level='DEBUG')
logger = logging.getLogger('__main__')


async def main():
    loop = asyncio.get_running_loop()

    # Stream from Zello -> USRP
    zousrp = AsyncByteStream()

    # Stream from USRP -> Zello
    usrpzo = AsyncByteStream()

    logger.info('Initialising Zello')
    zello = ZelloController(zousrp, usrpzo)

    logger.info('Initialising USRP')
    usrp = USRPController(usrpzo, zousrp)

    await asyncio.gather(*[
        zello.run(),

        # USRP tx
        usrp.run(),

        # Set up USRP rx
        loop.create_datagram_endpoint(lambda: usrp,
                                      local_addr=(os.environ.get('USRP_BIND'),
                                                  int(os.environ.get('USRP_RXPORT', 0))))
    ])

    print('waiting')
    loop.run_forever()

if __name__ == '__main__':
    asyncio.run(main())
