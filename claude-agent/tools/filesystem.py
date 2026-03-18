import os

MAX_READ_BYTES = 100 * 1024  # 100 KB


async def read_file(path: str) -> str:
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return f"ERROR: File not found: {path}"
    if not os.path.isfile(path):
        return f"ERROR: Not a file: {path}"
    size = os.path.getsize(path)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        contents = f.read(MAX_READ_BYTES)
    if size > MAX_READ_BYTES:
        contents += f"\n\n[truncated — file is {size} bytes, showed first {MAX_READ_BYTES}]"
    return contents


async def write_file(path: str, content: str) -> str:
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"OK: Wrote {len(content)} chars to {path}"


async def list_files(directory: str) -> str:
    directory = os.path.expanduser(directory)
    if not os.path.exists(directory):
        return f"ERROR: Directory not found: {directory}"
    if not os.path.isdir(directory):
        return f"ERROR: Not a directory: {directory}"
    entries = sorted(os.listdir(directory))
    lines = []
    for name in entries:
        full = os.path.join(directory, name)
        if os.path.islink(full):
            lines.append(f"[link]  {name}")
        elif os.path.isdir(full):
            lines.append(f"[dir]   {name}")
        else:
            size = os.path.getsize(full)
            lines.append(f"[file]  {name} ({size} bytes)")
    return "\n".join(lines) if lines else "(empty directory)"
