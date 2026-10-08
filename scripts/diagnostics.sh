#!/usr/bin/env bash
# Make the next crash diagnosable.  Run on the Pi:  sudo ./scripts/diagnostics.sh
#
# Raspberry Pi OS keeps the journal in RAM and discards core dumps, so a crash
# leaves nothing to work from: on player1 a SIGSEGV flooded the 15 MB RAM buffer
# with retry errors and rotated the crash itself out of history before anyone
# looked.  This turns on:
#
#   * a persistent journal, capped (survives reboots; 25 MB of retry spam can no
#     longer push out the interesting part)
#   * systemd-coredump, capped (a real backtrace via `coredumpctl info pipair`)
#
# Both are size-limited so they cannot fill the SD card.  Undo with
# --disable, which restores the stock behaviour.
set -euo pipefail

JOURNAL_MAX=${JOURNAL_MAX:-200M}
CORE_MAX=${CORE_MAX:-200M}
JOURNAL_DROPIN=/etc/systemd/journald.conf.d/pipair-diagnostics.conf
CORE_DROPIN=/etc/systemd/coredump.conf.d/pipair-diagnostics.conf

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

if [[ "${1:-}" == "--disable" ]]; then
  echo "==> Reverting to volatile logs and no core dumps"
  rm -f "$JOURNAL_DROPIN" "$CORE_DROPIN"
  rm -rf /var/log/journal
  systemctl restart systemd-journald
  echo "Done.  (systemd-coredump is left installed; apt-get remove it if you want.)"
  exit 0
fi

echo "==> Installing systemd-coredump"
if ! dpkg -s systemd-coredump >/dev/null 2>&1; then
  apt-get update </dev/null
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends systemd-coredump </dev/null
else
  echo "    already installed"
fi

echo "==> Persistent journal, capped at $JOURNAL_MAX"
install -d -m 0755 "$(dirname "$JOURNAL_DROPIN")"
cat >"$JOURNAL_DROPIN" <<EOF
# Installed by PiPair (scripts/diagnostics.sh).  Remove this file, or run
# the script with --disable, to go back to logs that live only in RAM.
[Journal]
Storage=persistent
SystemMaxUse=$JOURNAL_MAX
SystemMaxFileSize=20M
EOF
install -d -m 2755 -g systemd-journal /var/log/journal 2>/dev/null || install -d /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true
systemctl restart systemd-journald

echo "==> Core dumps, capped at $CORE_MAX"
install -d -m 0755 "$(dirname "$CORE_DROPIN")"
cat >"$CORE_DROPIN" <<EOF
# Installed by PiPair (scripts/diagnostics.sh).
[Coredump]
Storage=external
Compress=yes
MaxUse=$CORE_MAX
KeepFree=500M
EOF
systemctl daemon-reload
# The unit also needs LimitCORE=infinity; that ships in scripts/pipair.service.
if ! grep -q '^LimitCORE=' /etc/systemd/system/pipair.service 2>/dev/null; then
  echo "note: the installed pipair.service has no LimitCORE=infinity, so core dumps" >&2
  echo "      will still be discarded.  Re-run scripts/install.sh to update the unit." >&2
fi

echo
echo "Diagnostics enabled."
echo "  After a crash:   coredumpctl list"
echo "                   coredumpctl info pipair     # backtrace"
echo "  Logs now persist across reboots:  journalctl -u pipair -b -1"
echo "  Python-level stack on SIGSEGV is printed to the journal by faulthandler."
echo "  Disk currently used:  $(journalctl --disk-usage 2>/dev/null | sed 's/^.*take up //')"
