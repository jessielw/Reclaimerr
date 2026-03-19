from __future__ import annotations

import asyncio

from backend.core.logger import LOG
from backend.worker.work import do_the_thing

if __name__ == "__main__":
    try:
        asyncio.run(do_the_thing())
    except KeyboardInterrupt:
        LOG.info("Background worker received shutdown signal")
