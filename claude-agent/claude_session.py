import asyncio
import logging

from middleware import process as middleware_process

logger = logging.getLogger(__name__)

PER_CALL_TIMEOUT = 120    # seconds per individual claude invocation
TOOL_LOOP_TIMEOUT = 300   # seconds for the entire tool loop

SYSTEM_PROMPT = (
    "You are a capable agent. Use your built-in tools (Read, Write, Bash, etc.) as needed to complete the task.\n\n"
    "When you are done, you MUST respond with ONLY a JSON object — no text before or after, no markdown code fences. "
    "Use this exact structure:\n\n"
    "{\n"
    '  "thought": "your internal reasoning about what you did and why",\n'
    '  "actions": [\n'
    '    {"type": "read_file|write_file|shell|none", "input": "the path or command", "reason": "why you did it"}\n'
    "  ],\n"
    '  "final_answer": "your complete response to the user"\n'
    "}\n\n"
    "Rules for the actions array:\n"
    "- List every file read, file written, or shell command you ran.\n"
    '- If you took no file/shell actions, include exactly one entry: {"type": "none", "input": "", "reason": "no tools used"}.\n'
    "- Valid types: read_file, write_file, shell, none.\n"
    "- Every entry must have all three fields.\n\n"
    "Do not include any text outside the JSON object. Your entire response must be valid JSON."
)


class ClaudeSession:
    """
    Runs Claude via `claude --print` for each request.
    Conversation history is maintained between tool rounds using `--continue`.
    Concurrent requests are serialized through an asyncio.Lock.
    """

    def __init__(self):
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        pass  # no persistent process

    async def stop(self) -> None:
        logger.info("Claude session stopped")

    async def send(self, prompt: str) -> str:
        """Simple one-shot send with no tool loop."""
        async with self._lock:
            return await self._run_claude(["--system-prompt", SYSTEM_PROMPT], prompt)

    async def send_with_tools(self, prompt: str) -> str:
        async with self._lock:
            try:
                return await asyncio.wait_for(
                    self._tool_loop(prompt), timeout=TOOL_LOOP_TIMEOUT
                )
            except asyncio.TimeoutError:
                raise RuntimeError(f"Request timed out after {TOOL_LOOP_TIMEOUT}s")
            except Exception as exc:
                logger.error("send_with_tools failed: %s", exc)
                raise

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_claude(self, extra_args: list[str], input_text: str) -> str:
        """Spawn `claude --print [extra_args]`, send input via stdin, return stdout."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "--print", "--dangerously-skip-permissions", *extra_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            raise RuntimeError(f"Failed to launch claude: {e}")

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=input_text.encode()),
                timeout=PER_CALL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"Claude timed out after {PER_CALL_TIMEOUT}s")

        if stderr:
            logger.warning("Claude stderr: %s", stderr.decode(errors="replace").strip())

        return stdout.decode(errors="replace").strip()

    async def _tool_loop(self, prompt: str) -> str:
        response = await self._run_claude(["--system-prompt", SYSTEM_PROMPT], prompt)
        return middleware_process(prompt, response)
