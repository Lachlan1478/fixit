import asyncio
import logging

import middleware
from executor import execute_actions, format_results_for_claude

logger = logging.getLogger(__name__)

PER_CALL_TIMEOUT = 120    # seconds per individual claude invocation
TOOL_LOOP_TIMEOUT = 300   # seconds for the entire tool loop
MAX_TOOL_ROUNDS = 10

SYSTEM_PROMPT = (
    "You are a planning agent. You have NO tools and CANNOT execute anything yourself.\n"
    "To complete a task, propose actions and the server will execute them and return results.\n\n"
    "Always respond with ONLY a valid JSON object — no text before or after, no markdown fences:\n\n"
    "{\n"
    '  "thought": "your step-by-step reasoning",\n'
    '  "actions": [\n'
    '    {\n'
    '      "type": "read_file | write_file | shell | none",\n'
    '      "input": "file path or shell command",\n'
    '      "content": "file content — write_file ONLY, omit for all other types",\n'
    '      "reason": "why this action is needed"\n'
    '    }\n'
    "  ],\n"
    '  "final_answer": "set ONLY when all actions have type none — your complete answer to the user"\n'
    "}\n\n"
    "Workflow:\n"
    "1. To gather information or take action: list the actions you need. Leave final_answer empty or omit it.\n"
    "2. The server executes your actions and returns results in the next message.\n"
    "3. When you have everything you need: set actions to "
    '[{"type":"none","input":"","reason":"task complete"}] and write your full final_answer.\n\n'
    "Rules:\n"
    "- Valid types: read_file, write_file, shell, none.\n"
    "- write_file requires a \"content\" field with the complete file content.\n"
    "- You may propose multiple actions per round.\n"
    "- Do not include any text outside the JSON object. Your entire response must be valid JSON."
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
                "claude", "--print", *extra_args,
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
        response = await self._run_claude(
            ["--system-prompt", SYSTEM_PROMPT, "--allowedTools", ""], prompt
        )

        for round_num in range(MAX_TOOL_ROUNDS):
            parsed = middleware.parse(response)

            if parsed is None:
                middleware.log_round(prompt, round_num, None, response, is_final=True)
                logger.warning("Failed to parse response on round %d, returning raw", round_num)
                return response

            actions = middleware.validate_actions(parsed.get("actions", []))
            is_done = all(a["type"] == "none" for a in actions)
            middleware.log_round(prompt, round_num, parsed, response, is_final=is_done)

            if is_done:
                final_answer = parsed.get("final_answer", "")
                return final_answer.strip() if isinstance(final_answer, str) and final_answer.strip() else response

            logger.info("Round %d: executing %d action(s)", round_num + 1, len(actions))
            results = await execute_actions(actions)
            results_text = format_results_for_claude(results)
            response = await self._run_claude(["--continue", "--allowedTools", ""], results_text)

        logger.warning("Tool loop exhausted after %d rounds", MAX_TOOL_ROUNDS)
        return "I was unable to complete the task within the allowed number of steps."
