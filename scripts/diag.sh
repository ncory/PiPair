#!/usr/bin/env bash
# Hardware bring-up checks for PiPair. Run on the Pi:  sudo ./scripts/diag.sh [video.mp4]
# Stops the pipair service while testing (it needs the display) and restarts it afterwards.
set -uo pipefail

VIDEO="${1:-}"
ok() { printf '  \e[32mOK\e[0m   %s\n' "$*"; }
bad() { printf '  \e[31mFAIL\e[0m %s\n' "$*"; }
hdr() { printf '\n\e[1m%s\e[0m\n' "$*"; }

hdr "System"
echo "  $(tr -d '\0' </proc/device-tree/model 2>/dev/null)"
echo "  $(. /etc/os-release; echo "$PRETTY_NAME"), kernel $(uname -r), $(uname -m)"
echo "  memory: $(free -m | awk '/Mem:/ {print $2 " MB total, " $7 " MB available"}')"
echo "  CMA: $(grep -E 'CmaTotal|CmaFree' /proc/meminfo | tr -s ' ' | tr '\n' ' ')"
command -v vcgencmd >/dev/null && echo "  $(vcgencmd get_throttled) $(vcgencmd measure_temp)"

hdr "Display (DRM/KMS)"
ls -l /dev/dri/ 2>/dev/null | sed 's/^/  /'
for c in /sys/class/drm/card*-HDMI-A-*; do
  [[ -e $c ]] || continue
  echo "  $(basename "$c"): $(cat "$c/status"), modes: $(head -5 "$c/modes" | tr '\n' ' ')"
done
grep -q vc4-kms-v3d /boot/firmware/config.txt 2>/dev/null && ok "vc4-kms-v3d overlay enabled" || bad "dtoverlay=vc4-kms-v3d not found in config.txt"

hdr "GStreamer elements"
for el in glvideomixerelement glimagesink glupload glcolorconvert glcolorscale gldownload \
          decodebin v4l2h264dec v4l2slh265dec jpegdec imagefreeze audiomixer alsasink valve appsink; do
  if gst-inspect-1.0 "$el" >/dev/null 2>&1; then ok "$el"; else
    case $el in v4l2slh265dec) echo "  --   $el (HEVC; Pi 4/5 only)";; *) bad "$el";; esac
  fi
done

hdr "Audio"
cat /proc/asound/cards 2>/dev/null | sed 's/^/  /'

systemctl is-active --quiet pipair && { echo; echo "Stopping pipair for the tests..."; systemctl stop pipair; RESTART=1; }

HDMI=$(ls -d /sys/class/drm/card*-HDMI-A-* 2>/dev/null | while read -r c; do
  [[ $(cat "$c/status") == connected ]] && basename "$c" && break; done)
if [[ -n "$HDMI" ]]; then
  export GST_GL_WINDOW=gbm GST_GL_PLATFORM=egl GST_GL_API=gles2
  export GST_GL_GBM_DRM_DEVICE=/dev/dri/${HDMI%%-*} GST_GL_GBM_DRM_CONNECTOR=${HDMI#*-}
  hdr "GL output test (5 s of test pattern on $GST_GL_GBM_DRM_CONNECTOR)"
  if timeout 20 gst-launch-1.0 -q videotestsrc num-buffers=150 pattern=smpte ! \
      video/x-raw,width=1920,height=1080,framerate=30/1 ! glupload ! glimagesink >/tmp/diag-gl.log 2>&1; then
    ok "glimagesink over GBM/KMS works"
  else
    bad "glimagesink failed (see /tmp/diag-gl.log)"; tail -5 /tmp/diag-gl.log | sed 's/^/       /'
  fi
else
  bad "no connected HDMI display found"
fi

if [[ -n "$VIDEO" ]]; then
  hdr "Decode test: $VIDEO"
  gst-discoverer-1.0 "$VIDEO" 2>/dev/null | grep -E 'video|audio|Duration|Width|Height|Frame rate' | head -8 | sed 's/^/  /'
  echo "  hardware decode speed (no display):"
  timeout 120 gst-launch-1.0 -q filesrc location="$VIDEO" ! decodebin ! fpsdisplaysink video-sink=fakesink \
    text-overlay=false sync=false -v 2>&1 | grep -o 'average: [0-9.]*' | tail -1 | sed 's/^/    fps /'
  if [[ -n "$HDMI" ]]; then
    echo "  playing 10 s through the GL path (watch the screen)..."
    timeout 10 gst-launch-1.0 -q filesrc location="$VIDEO" ! decodebin ! glupload ! glcolorconvert ! glimagesink >/dev/null 2>&1
    ok "done (check that it looked smooth)"
  fi
fi

[[ "${RESTART:-0}" == 1 ]] && { echo; systemctl start pipair && echo "pipair restarted"; }
