import asyncio

MAX_OUTPUT_BYTES = 20 * 1024  # 20 KB
SHELL_TIMEOUT = 30


async def run_shell(command: str) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        return f"ERROR: Failed to launch command: {e}"

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=SHELL_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"ERROR: Command timed out after {SHELL_TIMEOUT}s: {command}"

    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")

    if stdout and stderr:
        combined = stdout + "\n[stderr]\n" + stderr
    else:
        combined = stdout or stderr

    if len(combined) > MAX_OUTPUT_BYTES:
        combined = combined[:MAX_OUTPUT_BYTES] + f"\n[truncated — output exceeded {MAX_OUTPUT_BYTES} bytes]"

    if proc.returncode != 0:
        combined += f"\n[exit code: {proc.returncode}]"

    return combined
