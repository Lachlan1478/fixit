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
    '      "type": "<see action types below>",\n'
    '      "input": "file path, shell command, git path, or commit message",\n'
    '      "content": "file content (write_file) or text to type (browser_fill) — omit for other types",\n'
    '      "reason": "why this action is needed"\n'
    '    }\n'
    "  ],\n"
    '  "final_answer": "set ONLY when all actions have type none — your complete answer to the user"\n'
    "}\n\n"
    "Action types:\n"
    "  read_file   — input: file path. Returns file contents.\n"
    "  write_file  — input: file path, content: complete new file content. Requires human approval.\n"
    "  shell       — input: shell command. Dangerous commands are blocked.\n"
    "  git_status  — input: empty. Returns working tree status.\n"
    "  git_diff    — input: optional file path for scoped diff, or empty for full diff.\n"
    "  git_add     — input: file path to stage.\n"
    "  git_commit  — input: commit message. Requires human approval.\n"
    "  browser_navigate     — input: URL (http/https only). Navigate browser. Auto-allowed.\n"
    "  browser_click        — input: CSS selector. Click element. Requires approval.\n"
    "  browser_fill         — input: CSS selector, content: text to type. Requires approval.\n"
    "  browser_extract_text — input: empty. Returns visible page text. Auto-allowed.\n"
    "  browser_screenshot   — input: optional filename hint. Saves PNG, returns path. Auto-allowed.\n"
    "  none        — use when task is complete. Set final_answer.\n\n"
    "Workflow:\n"
    "1. To gather information or take action: list the actions you need. Leave final_answer empty or omit it.\n"
    "2. The server executes your actions and returns results in the next message.\n"
    "3. When you have everything you need: set actions to "
    '[{"type":"none","input":"","reason":"task complete"}] and write your full final_answer.\n\n'
    "Rules:\n"
    "- git push is NOT available and will never be allowed. Do not propose it.\n"
    "- write_file requires a \"content\" field with the COMPLETE file content.\n"
    "- Always read a file before modifying it so you have the full current content.\n"
    "- You may propose multiple actions per round.\n"
    "- browser_fill requires a \"content\" field with the text to type.\n"
    "- Browser state persists across actions in the same task (navigate first, then interact).\n"
    "- file:// URLs and private/loopback addresses are blocked.\n"
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

    async def send_with_tools(self, prompt: str) -> dict:
        """Run the tool loop. Returns {"answer": str, "pending": list[dict]}."""
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

    async def _tool_loop(self, prompt: str) -> dict:
        """Run the agentic tool loop. Returns {"answer": str, "pending": list[dict]}."""
        response = await self._run_claude(
            ["--system-prompt", SYSTEM_PROMPT, "--allowedTools", "", "--dangerously-skip-permissions"], prompt
        )

        for round_num in range(MAX_TOOL_ROUNDS):
            parsed = middleware.parse(response)

            if parsed is None:
                middleware.log_round(prompt, round_num, None, response, is_final=True)
                logger.warning("Failed to parse response on round %d, returning raw", round_num)
                return {"answer": response, "pending": []}

            actions = middleware.validate_actions(parsed.get("actions", []))
            is_done = all(a["type"] == "none" for a in actions)
            middleware.log_round(prompt, round_num, parsed, response, is_final=is_done)

            if is_done:
                final_answer = parsed.get("final_answer", "")
                answer = final_answer.strip() if isinstance(final_answer, str) and final_answer.strip() else response
                return {"answer": answer, "pending": []}

            logger.info("Round %d: executing %d action(s)", round_num + 1, len(actions))
            results = await execute_actions(actions)

            review_results = [r for r in results if r.get("decision") == "review"]
            if review_results:
                pending = [
                    {"id": r["result"]["id"], "action": r["action"]}
                    for r in review_results
                ]
                logger.info(
                    "Round %d: %d action(s) require review — stopping loop",
                    round_num + 1, len(pending),
                )
                return {
                    "answer": "One or more actions require review before execution.",
                    "pending": pending,
                }

            results_text = format_results_for_claude(results)
            response = await self._run_claude(["--continue", "--allowedTools", "", "--dangerously-skip-permissions"], results_text)

        logger.warning("Tool loop exhausted after %d rounds", MAX_TOOL_ROUNDS)
        return {"answer": "I was unable to complete the task within the allowed number of steps.", "pending": []}
