"""
start.py — Launch the Assembly × Claude orchestrator.

Usage:
    python orchestrator/start.py
    python orchestrator/start.py --port 8002

From the claude_phone root directory.
"""

import os
import sys

# Ensure orchestrator dir is first on sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_AGENT_DIR = os.path.abspath(os.path.join(_HERE, "..", "claude-agent"))
_ASSEMBLY_DIR = os.path.abspath(os.path.join(_HERE, "..", "..", "Assembly"))

for _p in [_ASSEMBLY_DIR, _CLAUDE_AGENT_DIR, _HERE]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import argparse
import uvicorn

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Assembly × Claude orchestrator")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    print(f"\n  Assembly × Claude")
    print(f"  ─────────────────────────────────────────")
    print(f"  Orchestrator:  http://{args.host}:{args.port}")
    print(f"  Claude Agent:  http://localhost:8007  (run separately)")
    print(f"  Assembly UI:   http://localhost:8000  (run separately)")
    print(f"  ─────────────────────────────────────────\n")

    import server as srv
    uvicorn.run(
        srv.app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
