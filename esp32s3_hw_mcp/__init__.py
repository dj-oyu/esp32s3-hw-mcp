"""ESP32-S3 hardware knowledge MCP server.

Answers about the ESP32-S3's PIE instructions, registers and measured performance, each carrying the
document, version and printed page (or the device and run) it came from. The package ships the extracted
knowledge under ``esp32s3_hw_mcp/data`` so that

    uvx --from git+https://github.com/dj-oyu/esp32s3-hw-mcp esp32s3-hw-mcp

answers with no checkout, no PDFs and no network. ``esp32s3_hw_mcp.registry`` is the index of what is
reachable and how: which artifact, in which layer, served by which tool or resource, and exactly where each
root resolved to.
"""
from __future__ import annotations

__version__ = "0.2.0"

__all__ = ["__version__"]
