#!/usr/bin/env bash
# Make this Pi forget the device it is paired with.  Run on the Pi:
#
#   sudo /opt/pipair/scripts/unpair.sh
#
# For a head whose controller is gone (lost, re-installed, replaced): the head
# goes back to showing "Ready to pair". A controller can simply use Settings →
# Head pairing → Forget this head instead.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
systemctl stop pipair.service
cd /opt/pipair
sudo -u pipair PIPAIR_DATA=/var/lib/pipair python3 -m pipair --unpair
systemctl start pipair.service
