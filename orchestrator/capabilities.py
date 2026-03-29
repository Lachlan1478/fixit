"""
capabilities.py — Claude Code capabilities document.

Injected into Assembly personas' context so they design solutions that
Claude Code can realistically build.
"""

CAPABILITIES_DOC = """
## Claude Code Capabilities (the AI that will BUILD your solution)

Claude Code is an autonomous coding agent. When designing a solution, assume
it can do everything below without human intervention.

### File Operations
- Read any file, write/overwrite files, make targeted edits to existing files
- Search files by name pattern (glob) or content (regex grep)
- List directory trees

### Shell Execution
- Run any shell command: install packages (pip/npm/cargo), run servers, run tests,
  compile code, git operations, database migrations, etc.
- Commands time out after ~2 minutes each; complex tasks use multiple steps

### Web
- Fetch any public URL and read its content
- Search the web for documentation or examples
- Screenshot any running web app (Playwright) — screenshots appear inline in the UI

### Code Intelligence
- Spawn sub-agents for parallel subtasks (e.g. "write tests while writing implementation")
- Track progress with a todo list visible to the user

### Output conventions
- Web UIs should be written to `claude-agent/static/<name>.html` — they auto-preview live
- Python apps: use the workspace root as working directory
- Tests: pytest (Python), jest (JS) — run them and fix failures before declaring done

### What Claude Code CANNOT do
- It cannot interact with authenticated external services (no OAuth logins)
- It cannot run forever — each prompt is a discrete task
- It does not have production database credentials unless you provide them

### For spec/planning purposes
- Assume: Python 3.11, Node 18, git available
- MVPs should be buildable as a single self-contained app (no microservices)
- Prefer simple file-based storage (SQLite, JSON) over external databases for MVPs
- Target: working product after 1-3 Claude Code iterations
"""
