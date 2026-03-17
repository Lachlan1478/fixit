import asyncio
import logging

logger = logging.getLogger(__name__)

# How long to wait for more output before considering the response complete.
# Increase this if Claude's responses get cut off mid-stream.
SILENCE_TIMEOUT = 2.0


class ClaudeSession:
    """
    Manages a single persistent Claude CLI process.

    Prompts are serialized through an asyncio.Lock so concurrent API
    requests are queued rather than interleaved.  If the process dies
    it is restarted automatically before the next send.
    """

    def __init__(self):
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        await self._spawn()

    async def stop(self) -> None:
        await self._kill()
        logger.info("Claude session stopped")

    async def send(self, prompt: str) -> str:
        async with self._lock:
            if not self._alive():
                logger.warning("Claude is not running — restarting")
                await self._spawn()

            try:
                return await asyncio.wait_for(
                    self._do_send(prompt), timeout=120
                )
            except asyncio.TimeoutError:
                logger.error("Prompt timed out — restarting Claude")
                await self._restart()
                raise RuntimeError("Request timed out after 120 s")
            except Exception as exc:
                logger.error("Send failed (%s) — restarting Claude", exc)
                await self._restart()
                raise

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _spawn(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            "claude",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info("Claude started (PID %d)", self._process.pid)

    def _alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def _do_send(self, prompt: str) -> str:
        self._process.stdin.write((prompt + "\n").encode())
        await self._process.stdin.drain()
        return await self._collect_response()

    async def _collect_response(self) -> str:
        """
        Read stdout until SILENCE_TIMEOUT seconds pass with no new data.
        That silence signals Claude has finished its response.
        """
        chunks: list[str] = []
        while True:
            try:
                chunk = await asyncio.wait_for(
                    self._process.stdout.read(4096),
                    timeout=SILENCE_TIMEOUT,
                )
                if not chunk:
                    raise RuntimeError("Claude stdout closed unexpectedly")
                chunks.append(chunk.decode(errors="replace"))
            except asyncio.TimeoutError:
                break  # silence — response is complete

        return "".join(chunks).strip()

    async def _restart(self) -> None:
        await self._kill()
        await self._spawn()

    async def _kill(self) -> None:
        if self._process and self._process.returncode is None:
            self._process.kill()
            await self._process.wait()
