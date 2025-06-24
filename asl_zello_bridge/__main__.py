import asyncio
import logging
import os

from .zello import ZelloController
from .usrp import USRPController
from .stream import AsyncByteStream

log_level = os.environ.get('LOG_LEVEL', 'INFO')
log_format = os.environ.get('LOG_FORMAT', '%(levelname)s:%(name)s:%(message)s')
logging.basicConfig(level=log_level, format=log_format)
logger = logging.getLogger('__main__')


async def _main():
    loop = asyncio.get_running_loop()

    # Stream from Zello -> USRP
    zousrp = AsyncByteStream()

    # Stream from USRP -> Zello
    usrpzo = AsyncByteStream()

    usrp_ptt = asyncio.Event()
    zello_ptt = asyncio.Event()

    logger.info('Initialising Zello')
    zello = ZelloController(zousrp, usrpzo, usrp_ptt, zello_ptt)

    logger.info('Initialising USRP')
    usrp = USRPController(usrpzo, zousrp, usrp_ptt, zello_ptt)

    await asyncio.gather(*[
        zello.run(),

        # USRP tx
        usrp.run(),

        # Set up USRP rx
        loop.create_datagram_endpoint(lambda: usrp,
                                      local_addr=(os.environ.get('USRP_BIND'),
                                                  int(os.environ.get('USRP_RXPORT', 0))))
    ])

    loop.run_forever()

def main():
    asyncio.run(_main())

if __name__ == '__main__':
    main()
