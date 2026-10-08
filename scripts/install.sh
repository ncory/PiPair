#!/usr/bin/env bash
# PiPair system installer for Raspberry Pi OS Lite (Trixie / Debian 13).
#
# Usually run for you by the one-command installer (install.sh at the repo
# root) or by scripts/push.sh. To run it by hand from a checkout:
#
#   sudo ./scripts/install.sh --role head|controller [--deploy-user "$USER"]
#
# Re-run it to update: it re-syncs the code and restarts the service, keeping
# playlists, settings and media in /var/lib/pipair. The role is remembered,
# so --role is only needed the first time.
#
# Options:
#   --role ROLE           head: shows the picture (HDMI out), no web UI.
#                         controller: plays the sound, serves the web UI and API,
#                         and drives the head over Bluetooth.
#                         standalone: one Pi does both, like PiPlayer.
#   --no-quiet-boot       leave the boot console / login prompt on the HDMI output
#   --gpu-mem N           firmware video memory in MB (default 128 on Pi 4 and
#                         earlier with >=1GB RAM). 1080p dissolves need room for
#                         two decoders plus a freeze buffer; the 76MB stock
#                         setting leaves ~2MB and playback dies mid-transition.
#   --no-gpu-mem          never touch gpu_mem in config.txt
#   --port N              web UI port (default 80)
#   --deploy-user NAME    let NAME update PiPair without a password: NAME owns
#                         /opt/pipair and may start/stop/restart the service
#                         and reboot the Pi.
#                         (The service itself still runs as the unprivileged
#                         "pipair" user.)
set -euo pipefail

APP_DIR=/opt/pipair
DATA_DIR=/var/lib/pipair
SVC_USER=pipair
PORT=80
QUIET_BOOT=1
GPU_MEM=128        # 0 = leave config.txt alone
DEPLOY_USER=""
ROLE=""
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift ;;
    --no-quiet-boot) QUIET_BOOT=0 ;;
    --gpu-mem) GPU_MEM="$2"; shift ;;
    --no-gpu-mem) GPU_MEM=0 ;;
    --port) PORT="$2"; shift ;;
    --deploy-user) DEPLOY_USER="$2"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "--port must be a number" >&2; exit 2; }
[[ "$GPU_MEM" =~ ^[0-9]+$ ]] || { echo "--gpu-mem must be a number" >&2; exit 2; }
if [[ -n "$DEPLOY_USER" ]] && { [[ "$DEPLOY_USER" == root ]] || ! id "$DEPLOY_USER" >/dev/null 2>&1; }; then
  echo "--deploy-user: '$DEPLOY_USER' is not a regular user on this system" >&2; exit 2
fi
LOGIN_USER="${DEPLOY_USER:-${SUDO_USER:-}}"
UNIT=/etc/systemd/system/pipair.service
if [[ -z "$ROLE" && -f $UNIT ]]; then  # updating: keep the role this Pi already has
  ROLE="$(sed -n 's/^Environment=PIPAIR_ROLE=//p' "$UNIT" | tail -1)"
fi
case "$ROLE" in
  head|controller|standalone) ;;
  "") echo "say which half this Pi is: --role head  or  --role controller" >&2; exit 2 ;;
  *) echo "--role must be head, controller or standalone" >&2; exit 2 ;;
esac
# Only the device with the display needs its console hidden and video memory.
if [[ "$ROLE" == controller ]]; then QUIET_BOOT=0; GPU_MEM=0; fi
echo "==> Installing PiPair as: $ROLE"

echo "==> Installing packages"
apt-get update </dev/null
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends </dev/null \
  python3 python3-gi python3-gst-1.0 python3-aiohttp python3-pil python3-libgpiod \
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-gl gstreamer1.0-alsa \
  libgl1-mesa-dri libegl-mesa0 libgbm1 libgles2 \
  alsa-utils rsync avahi-daemon bluez python3-dbus-fast python3-bleak

echo "==> Creating user and directories"
if ! id "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SVC_USER"
fi
usermod -aG video,render,audio,gpio,bluetooth "$SVC_USER"
install -d -o "$SVC_USER" -g "$SVC_USER" "$DATA_DIR" "$DATA_DIR/media"
# let the login user drop files into the media folder (scp/rsync)
if [[ -n "$LOGIN_USER" && "$LOGIN_USER" != root ]]; then
  usermod -aG "$SVC_USER" "$LOGIN_USER"
  chmod 2775 "$DATA_DIR/media"
fi

echo "==> Installing application to $APP_DIR"
install -d "$APP_DIR"
rsync -a --delete --exclude .git --exclude .venv --exclude dev-data --exclude '__pycache__' \
  --exclude .pytest_cache --exclude .claude "$SRC_DIR/" "$APP_DIR/"

if [[ -n "$DEPLOY_USER" ]]; then
  echo "==> Letting $DEPLOY_USER deploy updates without a password"
  chown -R "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR"
  SUDOERS=/etc/sudoers.d/pipair-deploy
  TMP="$(mktemp)"
  SYSTEMCTL="$(command -v systemctl)"
  {
    echo "# Installed by PiPair (scripts/install.sh --deploy-user): lets $DEPLOY_USER"
    echo "# start/stop/restart the PiPair service and reboot the Pi, and nothing else,"
    echo "# without a password."
    echo "$DEPLOY_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart pipair.service, $SYSTEMCTL stop pipair.service, $SYSTEMCTL start pipair.service, $SYSTEMCTL reboot"
  } >"$TMP"
  if visudo -cf "$TMP" >/dev/null; then
    install -m 0440 -o root -g root "$TMP" "$SUDOERS"
  else
    echo "warning: generated sudoers rule failed validation; skipped" >&2
  fi
  rm -f "$TMP"
fi

if [[ "$ROLE" != standalone ]]; then
  echo "==> Setting up Bluetooth"
  command -v rfkill >/dev/null && rfkill unblock bluetooth || true
  systemctl enable bluetooth.service >/dev/null 2>&1 || true
  BT_CONF=/etc/bluetooth/main.conf
  if [[ "$ROLE" == controller && -f $BT_CONF ]]; then
    # The controller opens the connection, so its settings decide how often
    # the two radios meet. Short intervals (7.5-15 ms) keep the clock sync
    # tight; the stock 30-50 ms would put the picture ~20 ms early. A 2 s
    # supervision timeout notices a lost head quickly.
    cp -n "$BT_CONF" "$BT_CONF.pipair-backup" || true
    for kv in MinConnectionInterval=6 MaxConnectionInterval=12 ConnectionLatency=0 \
              ConnectionSupervisionTimeout=200; do
      key="${kv%%=*}"
      if grep -Eq "^[[:space:]]*#?[[:space:]]*$key[[:space:]]*=" "$BT_CONF"; then
        sed -i -E "0,/^[[:space:]]*#?[[:space:]]*$key[[:space:]]*=.*/s//$kv/" "$BT_CONF"
      else
        printf '\n[LE]\n%s\n' "$kv" >>"$BT_CONF"
      fi
    done
  fi
  systemctl restart bluetooth.service || echo "warning: could not restart bluetooth" >&2
fi

echo "==> Installing systemd service"
sed -e "s/@PORT@/$PORT/" -e "s/@ROLE@/$ROLE/" "$SRC_DIR/scripts/pipair.service" > "$UNIT"
systemctl daemon-reload
systemctl enable pipair.service

REBOOT_NEEDED=0

# Firmware video memory. The hardware H.264 decoder allocates from the firmware
# "reloc" heap, which gpu_mem sizes. At 1080p one clip costs ~26MB and a dissolve
# needs ~27MB more (two decoders plus the ISP freeze buffer), so the stock 76MB
# leaves about 2MB spare: the first dissolve scrapes through and a later one
# fails with "Failed to allocate required memory", taking the renderer down.
# Measured on a Pi 3B+ at 1080p; 128MB gives ~27MB of headroom instead.
# Pi 5 has no firmware decoder and ignores gpu_mem, so it is skipped there.
CONFIG_TXT=/boot/firmware/config.txt
if [[ $GPU_MEM -gt 0 && -f $CONFIG_TXT ]]; then
  MODEL="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || true)"
  RAM_MB=$(awk '/MemTotal:/ {print int($2/1024)}' /proc/meminfo)
  # sed, not grep -oP: prints nothing and still succeeds when there is no match,
  # so a failure here can never be mistaken for "unset" and append a duplicate.
  CUR_GPU_MEM="$(sed -n 's/^[[:space:]]*gpu_mem[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' \
                     "$CONFIG_TXT" | tail -1)"
  if [[ "$MODEL" == *"Raspberry Pi 5"* ]]; then
    : # no firmware video decoder; gpu_mem does nothing
  elif [[ -n "$CUR_GPU_MEM" ]]; then
    if [[ "$CUR_GPU_MEM" -lt "$GPU_MEM" ]]; then
      echo "note: config.txt already sets gpu_mem=$CUR_GPU_MEM; leaving it alone." >&2
      echo "      1080p dissolves may run out of video memory below ${GPU_MEM}MB." >&2
    fi
  elif [[ -z "$RAM_MB" || "$RAM_MB" -lt 900 ]]; then
    echo "note: only ${RAM_MB}MB RAM; not reserving ${GPU_MEM}MB for video." >&2
  else
    echo "==> Reserving ${GPU_MEM}MB of video memory (gpu_mem, needs a reboot)"
    cp -n "$CONFIG_TXT" "$CONFIG_TXT.pipair-backup" || true
    # Its own [all] section, so it applies whatever conditional block precedes it.
    printf '\n[all]\ngpu_mem=%s\n' "$GPU_MEM" >>"$CONFIG_TXT"
    REBOOT_NEEDED=1
  fi
fi

if [[ $QUIET_BOOT -eq 1 ]]; then
  echo "==> Quiet boot: hiding console text and the tty1 login prompt"
  CMDLINE=/boot/firmware/cmdline.txt
  if [[ -f $CMDLINE ]]; then
    cp -n "$CMDLINE" "$CMDLINE.pipair-backup" || true
    for opt in quiet loglevel=3 logo.nologo vt.global_cursor_default=0 consoleblank=0; do
      grep -qw -- "$opt" "$CMDLINE" || sed -i "1 s/\$/ $opt/" "$CMDLINE"
    done
  fi
  systemctl disable getty@tty1.service >/dev/null 2>&1 || true
  REBOOT_NEEDED=1
fi

systemctl restart pipair.service
sleep 2
systemctl --no-pager --lines=5 status pipair.service || true

HOST="$(hostname).local"
[[ "$PORT" == 80 ]] && URL="http://$HOST/" || URL="http://$HOST:$PORT/"
echo
if [[ "$ROLE" == head ]]; then
  echo "PiPair head is installed. It advertises over Bluetooth as '$(hostname)' and"
  echo "waits for a controller. Copy the same media files here that the controller has:"
  echo "  scp clip.mp4 ${LOGIN_USER:-user}@$HOST:$DATA_DIR/media/"
else
  echo "PiPair $ROLE is installed. Open $URL"
fi
echo "Media folder: $DATA_DIR/media   Logs: journalctl -u pipair -f"
[[ $REBOOT_NEEDED -eq 1 ]] && echo "Reboot once to apply the boot settings:  sudo reboot"
