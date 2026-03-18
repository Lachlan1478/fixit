import asyncio

MAX_OUTPUT = 20 * 1024
GIT_TIMEOUT = 30


async def _run_git(args: list[str], cwd: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    except Exception as e:
        return f"ERROR: Failed to launch git: {e}"

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=GIT_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"ERROR: git timed out after {GIT_TIMEOUT}s"

    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace").strip()
    combined = stdout + (f"\n[stderr]\n{stderr}" if stderr else "")
    if len(combined) > MAX_OUTPUT:
        combined = combined[:MAX_OUTPUT] + "\n[truncated]"
    if proc.returncode != 0:
        combined += f"\n[exit code: {proc.returncode}]"
    return combined.strip()


async def git_status(workspace: str) -> str:
    return await _run_git(["status", "--short", "--branch"], workspace)


async def git_diff(workspace: str, path: str = "") -> str:
    args = ["diff"]
    if path:
        args += ["--", path]
    return await _run_git(args, workspace)


async def git_add(workspace: str, path: str) -> str:
    return await _run_git(["add", "--", path], workspace)


async def git_commit(workspace: str, message: str) -> str:
    return await _run_git(["commit", "-m", message], workspace)
