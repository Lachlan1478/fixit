"""
Path-traversal security tests — /file, /image, /files endpoints.

Verifies that:
- Paths outside WORKSPACE_ROOT are rejected with the correct status code.
- Valid paths inside WORKSPACE_ROOT are served normally.
"""

import os

import pytest


# WORKSPACE_ROOT is the parent of claude-agent/ (i.e. the repo root)
_HERE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
WORKSPACE_ROOT = os.path.abspath(os.path.join(_HERE, ".."))


# ── /files path traversal ─────────────────────────────────────────────────────

@pytest.mark.api
async def test_files_traversal(client):
    """Path that escapes workspace → 400."""
    response = await client.get("/files?path=../../etc")
    assert response.status_code == 400


@pytest.mark.api
async def test_files_root_is_valid(client):
    """Root files listing (no path) always succeeds."""
    response = await client.get("/files")
    assert response.status_code == 200


# ── /image path traversal ─────────────────────────────────────────────────────

@pytest.mark.api
async def test_image_traversal(client):
    """Traversal to a path outside workspace → 403."""
    response = await client.get("/image?path=../../etc/shadow.png")
    assert response.status_code == 403


@pytest.mark.api
async def test_image_traversal_windows_style(client):
    """Windows-style separator traversal → 403."""
    response = await client.get("/image?path=..\\..\\etc\\passwd.png")
    assert response.status_code == 403


@pytest.mark.api
async def test_image_absolute_outside_workspace(client):
    """Absolute path outside workspace → 403."""
    if os.name == "nt":
        outside = "C:\\Windows\\System32\\drivers\\etc\\hosts.png"
    else:
        outside = "/etc/passwd.png"
    response = await client.get(f"/image?path={outside}")
    assert response.status_code == 403


@pytest.mark.api
async def test_image_absolute_inside_workspace(client):
    """
    An absolute path that IS inside workspace should pass the boundary check
    (but may 404 if the image doesn't exist, or 400 if not an image extension).
    The key is that it does NOT return 403 (boundary rejection).
    """
    # Use test-run.png from workspace root if it exists, else any .png we know about
    candidate = os.path.join(WORKSPACE_ROOT, "test-run.png")
    if not os.path.isfile(candidate):
        pytest.skip("test-run.png not found in workspace root — skipping absolute-inside test")

    response = await client.get(f"/image?path={candidate}")
    # Should NOT be 403 (forbidden) — it's inside the workspace
    assert response.status_code != 403


# ── /file path traversal ─────────────────────────────────────────────────────

@pytest.mark.api
async def test_file_traversal(client):
    """Traversal path for /file → 400."""
    response = await client.get("/file?path=../../etc/passwd")
    assert response.status_code == 400


@pytest.mark.api
async def test_file_traversal_encoded(client):
    """URL-encoded traversal → 400."""
    # %2F is /, so ../../etc/passwd encoded
    response = await client.get("/file?path=..%2F..%2Fetc%2Fpasswd")
    assert response.status_code == 400


@pytest.mark.api
async def test_file_absolute_outside_workspace(client):
    """Absolute path outside workspace → 400."""
    if os.name == "nt":
        outside = "C:\\Windows\\System32\\drivers\\etc\\hosts"
    else:
        outside = "/etc/passwd"
    response = await client.get(f"/file?path={outside}")
    assert response.status_code == 400


# ── Additional edge-case traversal vectors ────────────────────────────────────

@pytest.mark.api
async def test_image_double_encoded_traversal(client):
    """Double-encoded traversal (%252F = literal %2F after first decode) → 403."""
    # The server receives the literal string "..%2F..%2Fetc%2Fpasswd.png"
    # and normpath should still catch it.  Either 403 (boundary) or 400 (invalid)
    # is acceptable; the key is NOT 200.
    response = await client.get("/image?path=..%252F..%252Fetc%252Fpasswd.png")
    assert response.status_code in (400, 403, 404)


@pytest.mark.api
async def test_file_null_byte_injection(client):
    """Null-byte injection in path (classic extension bypass) → not 200."""
    # urllib sends the literal %00 as a query param; the server should reject
    # or safely normalize paths that contain null bytes.
    response = await client.get("/file?path=safe%00evil")
    # Any non-200 response is acceptable; a 200 with null in the path is a bug.
    assert response.status_code != 200 or response.json().get("exists") is False


@pytest.mark.api
async def test_files_traversal_with_normpath_bypass(client):
    """Mixed traversal with redundant slashes → 400 (boundary check)."""
    # os.path.normpath("//../../etc") resolves to "../../etc" on POSIX
    # and the boundary check must catch it.
    response = await client.get("/files?path=//../../etc")
    assert response.status_code == 400
