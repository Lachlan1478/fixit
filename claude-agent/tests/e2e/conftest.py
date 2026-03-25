"""
E2E test fixtures — project config + live server URL.

The PROJECT_CONFIG dict is the only thing you need to change to reuse
the build harness for a different project.
"""

import pytest

PROJECT_CONFIG = {
    "name": "color_picker",
    "build_prompt": (
        "Create claude-agent/static/color_picker.html — a self-contained color picker "
        "app. Include three range sliders for Hue (0-360), Saturation (0-100%), and "
        "Brightness (0-100%). Show the resulting color as a large swatch and display "
        "the corresponding hex code. Use only vanilla HTML/CSS/JS in one file."
    ),
    "output_path": "claude-agent/static/color_picker.html",
    "output_url": "/static/color_picker.html",
    "screenshot_url": "http://localhost:8007/static/color_picker.html",
    "screenshot_name": "color_picker",
    "follow_up_prompt": (
        "Add a 'Random color' button that picks a random hue, saturation, and brightness "
        "and updates all sliders and the swatch accordingly."
    ),
}


@pytest.fixture(scope="module")
def project_config():
    return PROJECT_CONFIG


# Re-use the live_server_url fixture from the parent conftest
# (pytest automatically finds it via conftest.py hierarchy)
