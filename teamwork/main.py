from __future__ import annotations

import asyncio

from reprojourney.agents.main_agent import run_cli_session


if __name__ == "__main__":
    asyncio.run(run_cli_session())
