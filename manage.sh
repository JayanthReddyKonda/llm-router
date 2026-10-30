#!/usr/bin/env bash
# ==============================================================================
# LiteLLM Dynamic Provider & Model Manager (TUI)
#
# Launches the interactive terminal interface to manage:
#   • Providers (Base URLs)
#   • Multi-Key pools with individual RPM rate limits
#   • Model aliases & deployments
#
# Usage:
#   ./manage.sh
# ==============================================================================

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Launch the interactive terminal UI
python3 "$SCRIPT_DIR/manager/provider_manager.py" "$@"
