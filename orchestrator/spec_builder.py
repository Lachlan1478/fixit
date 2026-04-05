"""
spec_builder.py — Convert Assembly's ConvergenceOutput into a Claude Code prompt.
"""

import re


def _slug(name: str) -> str:
    """Turn a product name into a safe filename slug."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def build_prompt(convergence: dict, purpose: str = "product") -> str:
    """
    Convert a ConvergenceOutput dict into a Claude Code implementation prompt.

    Args:
        convergence: ConvergenceOutput.to_dict() result from Assembly
        purpose: "product" | "personal" — adjusts framing of the prompt

    Returns:
        A detailed, actionable prompt for Claude Code
    """
    name = convergence.get("product_name", "Tool")
    pitch = convergence.get("one_sentence_pitch", "")
    mvp = convergence.get("mvp_bullets", [])
    not_doing = convergence.get("what_we_are_not_doing", [])
    slug = _slug(name)

    mvp_lines = "\n".join(f"  - {b}" for b in mvp) if mvp else "  - (see pitch)"
    not_doing_lines = "\n".join(f"  - {x}" for x in not_doing) if not_doing else ""

    if purpose == "personal":
        header = f"# Build: {name} (personal tool)"
        pitch_label = "**What it does:**"
        features_label = "## Features to build (ALL of these)"
    else:
        header = f"# Build: {name}"
        pitch_label = "**Pitch:**"
        features_label = "## MVP Features (build ALL of these)"

    sections = [
        header,
        "",
        f"{pitch_label} {pitch}",
        "",
        features_label,
        mvp_lines,
    ]

    if not_doing_lines:
        sections += ["", "## Out of scope (do NOT build these)", not_doing_lines]

    sections += [
        "",
        "## Delivery requirements",
        f"1. Write the main UI to `claude-agent/static/{slug}.html` so it previews live.",
        "2. Write backend code (if needed) to the workspace root.",
        "3. Write tests and run them — fix all failures before finishing.",
        "4. Take a screenshot of the running app at the end.",
        "5. Finish with a brief summary of what was built and how to run it.",
        "",
        "Start building now.",
    ]

    return "\n".join(sections)


def build_followup_prompt(convergence: dict, iteration: int, reviewer_notes: list[str]) -> str:
    """
    Build a follow-up prompt for iteration N based on reviewer feedback.

    Args:
        convergence: Original ConvergenceOutput dict
        iteration: Current iteration number (1-based)
        reviewer_notes: List of follow_up strings from reviewer verdicts

    Returns:
        A prompt asking Claude to address specific issues
    """
    name = convergence.get("product_name", "Product")
    notes_lines = "\n".join(f"- {n}" for n in reviewer_notes if n)

    return f"""# Fix & improve: {name} (iteration {iteration})

The initial build has been reviewed. Please address the following issues:

{notes_lines}

After fixing, re-run all tests and take a new screenshot.
Do not rewrite everything from scratch — make targeted fixes only.
"""
