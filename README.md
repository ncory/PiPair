# PiPair

Two Raspberry Pis that play one show: the **head** shows the picture, the **controller** plays the sound, and a Bluetooth link keeps them together. Built for a projector hidden inside a prop, running on a battery, with the speakers and network somewhere else.

PiPair is a fork of [PiPlayer](https://github.com/ncory/PiPlayer) and keeps its playlists, transitions, web UI and API.

- **Head**: a Pi with HDMI out to the projector. No network needed during the show, no web UI. It shows what the controller tells it to, timed on its own clock.
- **Controller**: a Pi on wired Ethernet with speakers attached. It serves the web UI and the REST + WebSocket API, plays the sound of each item, and drives the head.
- **Link**: Bluetooth LE. It carries only clock pings and small commands ("start this clip at time T"), never the media, so it needs almost no bandwidth and tolerates a crowded 2.4 GHz band.
- **Playlists** of videos and still images, with **cut**, **dissolve** and **dip to color** transitions, per-item position offsets, GPIO triggers, and a live preview of the head's picture.
- If the link drops, the sound carries on and the head keeps showing, and loops, the clip it had. When the link returns the current item restarts on both, together.
- **Pairing**: a controller and a head are paired once, with a code shown on the head's display. After that each talks only to the other, so several sets can run side by side.
- The web UI and API have no authentication. Meant for a trusted LAN.

## Hardware

| | Head | Controller |
|---|---|---|
| Tested target | Pi 3 Model B / B+ (Zero 2 W and 3 A+ use the same chip) | Pi 3 Model B / B+ |
| Needs | HDMI out, onboard Bluetooth | Ethernet, onboard Bluetooth, audio out |
| Content | H.264, up to 1080p30 (720p suits a pico projector) | the same files, for their sound |

Sound comes from a USB or HAT sound card if the controller has one, otherwise the 3.5 mm jack. Change it under Settings → Audio.

## Setting up the two Pis

Use **Raspberry Pi OS Lite (64-bit), Trixie** on both. In Raspberry Pi Imager's OS customization, give them different hostnames (say `pipair-head` and `pipair`), a user, and SSH. The head needs a network only for setup and for copying media; give it Wi-Fi in the Imager or plug it in for now.

Install from this checkout on your computer (asks for each Pi's password):

```bash
./scripts/push.sh you@pipair-head.local head
```

```bash
./scripts/push.sh you@pipair.local controller
```

Reboot both once, then pair them (next section). The pill in the top bar of `http://pipair.local/` reads **Head connected** when the two are in step.

Run the same `push.sh` command again to update; the role is remembered, and playlists, settings and media are kept.

Once the repository is on GitHub, a Pi can also install itself:

```bash
curl -sL "https://github.com/ncory/PiPair/raw/refs/heads/main/install.sh" | PIPAIR_ROLE=head bash
```

The installer:
- installs GStreamer, BlueZ and the Python packages (all from Debian; nothing from PyPI)
- creates a `pipair` service user and the `pipair` systemd service, with data in `/var/lib/pipair`
- **head:** hides boot messages and the login prompt on the HDMI output, and reserves 128 MB of video memory
- **controller:** serves the UI on port 80 (`--port N` to change), and sets BlueZ to a 7.5-15 ms connection interval in `/etc/bluetooth/main.conf` (the original is kept as `main.conf.pipair-backup`)

### Video memory (`gpu_mem`)

The hardware H.264 decoder allocates from the firmware's video memory pool, which `gpu_mem` sizes. At 1080p one clip costs about 26 MB, and a dissolve needs roughly 27 MB more, because two clips and the freeze buffer are alive at once. Raspberry Pi OS ships with `gpu_mem=76`, which leaves about 2 MB spare: the first dissolve scrapes through and a later one fails with `Failed to allocate required memory`, taking the renderer down until the Pi is rebooted.

The installer therefore appends `gpu_mem=128` to `/boot/firmware/config.txt` (backing the file up first), which roughly doubles the pool and leaves around 27 MB of headroom. It applies at the next reboot. The installer never overrides a `gpu_mem` you have set yourself; it warns instead if yours is lower. It is skipped on the Pi 5, which has no firmware decoder, and on boards with less than 1 GB of RAM. Use `--no-gpu-mem` to leave `config.txt` untouched.

## Pairing

A new head shows its name and **Ready to pair** on its display. To pair:

1. Open the controller's web UI → Settings → **Head pairing**. Heads in range are listed with their name, Bluetooth address and signal strength.
2. Click **Pair** next to yours. The head's display changes to a 4-digit **pairing code**, and the same code appears in the UI.
3. If they match, click **Yes, the codes match**. If the head shows a different code or none, you picked another head: cancel.

That is all, and it is kept across reboots. From then on the controller connects only to that head, the head obeys only that controller, and a paired head never puts text on its display.

To change partners, use **Forget this head** on the controller. That also releases the head if it is in range. A head whose controller is gone can be released on the head itself:

```bash
sudo /opt/pipair/scripts/unpair.sh
```

What pairing protects against: another PiPair controller (or anyone with a Bluetooth radio) connecting to your head and driving it, and your controller latching onto somebody else's head. Each connection starts with both sides proving they hold the shared key, and every message after that is signed and numbered, so forged, altered or replayed commands are dropped. Messages are not encrypted, since nothing in them is secret, and nothing stops a radio jammer.

## Media

Every file has to be on **both** devices under the same name: the controller plays its sound, the head shows its picture.

1. Upload it in the web UI (Media tab). That puts it on the controller.
2. Copy the same file to the head:
   ```bash
   scp clip.mp4 you@pipair-head.local:/var/lib/pipair/media/
   ```

The Media tab and the playlist editor flag files the head doesn't have (or has in a different size). The head reports its files every few seconds, so the flag clears by itself.

Convert anything that isn't H.264 before uploading:

```bash
ffmpeg -i input.mov -c:v libx264 -profile:v high -level 4.0 -pix_fmt yuv420p \
  -vf "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30" \
  -preset slow -crf 20 -g 60 -movflags +faststart -c:a aac -b:a 192k -ar 48000 output.mp4
```

## Lip sync

The controller pings the head a few times a second and works out the difference between their clocks, so "start at T" means the same instant on both. What it can't know is how long your projector takes to show a frame or your amplifier to play a sample. Settings → Head → **Picture offset** covers that: a positive number shows the picture later, a negative one earlier. Set it once with the real projector and speakers; it is saved.

Now Playing → Player shows the link's round-trip time. Around 15-30 ms is normal. The clock estimate uses the fastest recent ping, so occasional slow ones don't matter.

Each item is started in step, and the two devices then run on their own clocks until the next item. Crystals differ by a few parts per million, so a very long clip can drift by some milliseconds per minute; clips of a few minutes stay well inside what the eye notices.

## Display modes

Settings → *Display mode* applies to the head's display. It is `auto` or any mode the display offers (for example `1920x1080@60`).

- `auto` uses the display's preferred resolution. On a Pi 3 it picks the 30 Hz (or else 25 Hz) version of that resolution when the display supports it. The Pi 3's display controller has a fixed pixel budget: at 60 Hz it can show only one 1080p video at a time, so a dissolve between two videos would turn into a cut. At 30 Hz two fit comfortably, and 30 fps content plays without judder.
- Where that isn't possible (a display that only accepts 50/60 Hz, or a 60 Hz mode you chose), a Pi 3 can't show two 1080p videos at once. Video-to-video dissolves then **freeze the outgoing clip**: it holds its current frame (converted to a still by the Pi's image processor) while the new clip fades in over it with full motion, about ¼ second after the freeze. Cuts, loops with cuts, dips to color, and dissolves to or from images aren't affected. The Playlists page shows a notice when this applies. If the display doesn't list 30 Hz, try forcing `1920x1080@30` in Settings → Display mode: many projectors and TVs accept it anyway.

If the display isn't connected at boot, PiPair retries every 30 s and resumes the default playlist once a display appears. To make HDMI come up even when the display is off, add `video=HDMI-A-1:1920x1080@30D` to the end of the single line in `/boot/firmware/cmdline.txt`. The trailing `D` forces the output on.

## GPI triggers (contact closures)

Set these up on the web UI's Triggers tab, or with `PUT /api/gpi`. Each input watches one GPIO pin and runs an action when its contact **closes**, **opens**, or either. The actions are: play a playlist (optionally from a given item, with a transition), next, previous, stop, pause, resume, pause/resume, and loop item.

Wiring: connect a switch, button or dry relay contact between a GPIO pin and a GND pin. The Pi's internal pull-up holds the pin high, and closing the contact pulls it low. No other parts are needed.

| Good GPIO choices | Header pin |  | GND header pins |
|---|---|---|---|
| GPIO 17, 27, 22 | 11, 13, 15 | | 6, 9, 14, 20, 25, 30, 34, 39 |
| GPIO 23, 24, 25 | 16, 18, 22 | | |
| GPIO 5, 6, 16, 26 | 29, 31, 36, 37 | | |

- The pins are 3.3 V only. **Never connect 5 V or 12 V to a GPIO pin.** For a device that outputs a voltage (such as a show controller's 12 V GPO), use a relay or an optocoupler, or set the input to pull-down / active high for a 3.3 V logic signal.
- For long cable runs, twisted pair and the kernel debounce (20 ms by default) keep noise out. The hold-off (300 ms by default) ignores re-triggers, such as a double-pressed button.
- One pin can carry several inputs, for example *closes* plays one playlist and *opens* plays another.
- The Triggers tab shows each input's live open/closed state, so you can check the wiring. **Test** runs an input's action without touching the hardware.

## Development (any machine)

```bash
python3 -m venv .venv && .venv/bin/pip install aiohttp pillow pytest pytest-asyncio
.venv/bin/python -m pipair --backend sim --data dev-data      # http://localhost:8080
.venv/bin/python -m pytest
```

Off a Pi, the controller runs a simulated head inside the same process, joined by a loopback link that uses the real message format. The UI, API, playlist logic and the controller/head protocol can all be worked on without hardware. `--role standalone` runs the original single-device player.

## How it works

```
pipair/
  web.py             aiohttp REST API, WebSocket status, static UI      (controller)
  engine.py          playlists, scheduling, transitions, error recovery (controller)
  backend_pair.py    the controller's renderer: sound here, picture on the head
  backend_audio.py   audio-only GStreamer renderer                      (controller)
  head.py            executes the controller's commands on a video renderer (head)
  backend_kms.py     hardware-plane video renderer                      (head)
  link.py            message format, packet framing, signing, clock sync, loopback link
  pairing.py         pairing, per-connection authentication, who-talks-to-whom
  ble_central.py     Bluetooth LE transport, controller side (bleak)
  ble_peripheral.py  Bluetooth LE transport, head side (BlueZ GATT server over D-Bus)
  backend_gst.py     GStreamer layer machinery shared by the renderers
  backend_sim.py     simulated renderer for development and tests
  config.py, media.py, gpi.py, hw.py, kms.py, static/
```

The engine already describes playback as "start this layer at running time T" plus opacity and volume keyframes. PairBackend splits each of those calls: volume and audio start go to the local audio renderer, everything about the picture is sent to the head with its times translated into the head's clock.

```
controller                                          head
engine ─ PairBackend ─┬─ AudioBackend ─ speakers
                      └─ HeadPort ═══ BLE ═══ Head ─ KmsBackend ─ HDMI ─ projector
                         clock sync, commands        (same renderer as PiPlayer, audio off)
```

- **Clock sync.** Each ping carries four timestamps (sent, received, reply sent, reply received). The sample with the shortest round trip in the last ~12 s gives the offset between the two monotonic clocks. Time a message spends queued is excluded, because the timestamps are taken as it leaves.
- **Commands.** A handful of small JSON messages: `new` (load a file), `start` (at T), `alpha` (keyframes), `dip`, `pos`, `pause`/`resume`, `rm`. A message is split into BLE packets with a one-byte header and reassembled on the other side.
- **Scheduling.** Cues are scheduled 0.25-0.6 s ahead so they cross the radio before their time comes.
- **Images** are fitted to the display on the head, at its resolution.
- **Pairing.** An X25519 key exchange; the controller commits to its key before seeing the head's, so the 4-digit code is enough to rule out someone in the middle. The shared key is stored in `pairing.json` on each device. Every connection then runs a three-message challenge-response and derives two session keys; each message carries a counter and an 8-byte HMAC. It all sits between the transport and the rest of the program (`pairing.py`), which only ever sees "connected" once the partner has proved itself.
- **Losing the head.** The controller treats a missing head as "nothing to wait for": the engine keeps running on sound alone. The head, hearing no pings for 3 s, loops the clip on screen when it ends. A head that (re)appears makes the engine restart the current item on a fresh renderer, which is the only way to start both together.

The video path on the head is PiPlayer's: decoded frames go straight onto hardware display planes, and the display controller positions, scales and alpha-blends them. See PiPlayer's README for the details.

## Troubleshooting

- **Logs:** `journalctl -u pipair -f` on either Pi.
- **"No head paired":** pair one under Settings → Head pairing. If the list is empty, check the head's service is running (`systemctl status pipair`) and its log says `advertising as '...'`. `bluetoothctl show` on each Pi should say `Powered: yes`.
- **"Searching for head…":** the paired head is off or out of range.
- **"Head refused us":** the head was reset or paired with another controller. Forget it on the controller and pair again (release the head with `unpair.sh` first if it still belongs to someone else).
- **"Head has no display":** the projector is off or unplugged. The head retries every 30 s. To make HDMI come up regardless, see Display modes.
- **Picture and sound out of step by a steady amount:** set the Picture offset.
- **Round trip well above 50 ms:** the connection interval setting may not have applied. Check `MinConnectionInterval`/`MaxConnectionInterval` in `/etc/bluetooth/main.conf` on the controller and reboot it.
- **No sound:** Settings → Audio. `aplay -l` on the controller lists the cards.
- **A file plays sound but no picture:** it isn't on the head (the Media tab says so).

## Status

The playlist engine, web UI and video renderer come from PiPlayer, which was brought up on a Pi 3 B+ with Trixie.

New in PiPair, and tested so far only on a simulated two-device rig (`tests/test_pair.py`, `tests/test_link.py`, `tests/test_pairing.py`): the controller/head protocol, clock sync, resync and link-loss behaviour, pairing and signed sessions.

**Not yet run on hardware:** the two Bluetooth transports and the audio-only renderer. Expect to debug those on the first bring-up.

Not done yet: copying media to the head from the web UI (use `scp` for now).

## License

MIT. See [LICENSE](LICENSE).
