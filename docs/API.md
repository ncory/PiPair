# PiPair HTTP API

Base URL: `http://<pi-address>/` (port 80 when installed as a service, 8080 when run by hand).

- Requests and responses are JSON (`Content-Type: application/json`).
- There is **no authentication**. PiPair is meant for a trusted LAN only.
- CORS is open (`Access-Control-Allow-Origin: *`), so any web page or control system can call it.
- Errors return a non-2xx status and `{"error": "message"}` (400 = invalid input, 404 = unknown playlist/media, 409 = conflict).
- Transport commands accept **POST or GET**. Parameters can be sent as a JSON body or as query-string parameters, so simple show controllers that can only fire a URL work too, for example `GET /api/playlists/lobby/play?transition=dissolve&duration=2`.

## Concepts

**Playlist**: an ordered list of items plus playback rules:

```json
{
  "id": "lobby",
  "name": "Lobby Loop",
  "items": [
    {"uid": "a1b2c3d4", "media": "welcome.mp4", "duration": null, "transition": null, "fit": null,
     "offset_x": 0, "offset_y": 0},
    {"uid": "e5f6a7b8", "media": "menu.png", "duration": 8, "transition": {"type": "cut"}, "fit": "cover",
     "offset_x": -40, "offset_y": 12}
  ],
  "transition": {"type": "dissolve", "duration": 1.0, "color": "#000000"},
  "end_action": {"type": "goto", "playlist": "attract", "transition": null}
}
```

| Field | Meaning |
|---|---|
| `id` | 1-64 characters: lowercase letters, digits, `-`, `_`. Used in URLs. Can be changed; references (default playlist, goto) follow. |
| `name` | Display name. |
| `items[].media` | File name in the media library. |
| `items[].duration` | Seconds to show an **image** (`null` = settings `default_image_duration`). Ignored for videos, which play to their end. |
| `items[].transition` | Transition *into* this item (`null` = the playlist's transition). |
| `items[].fit` | `contain` (letterbox), `cover` (fill and crop), `stretch`, or `null` (settings `default_fit`). |
| `items[].offset_x`, `items[].offset_y` | Shift the item this many pixels right/down (negative = left/up), in compositing-resolution pixels. The background color shows in any area uncovered. Changes apply live to the item on screen, including while paused. |
| `items[].uid` | Stable item id, generated if omitted. Keep it when editing so playback can track the current item. |
| `transition` | Transition between items in this playlist (`null` = settings `default_transition`). |
| `end_action.type` | `loop` (default), `stop` (transition to the background color), `hold` (freeze on the last frame), `goto` (start another playlist). |
| `end_action.playlist` | Target playlist id for `goto`. |
| `end_action.transition` | Transition into the `goto` playlist (`null` = that playlist's first-item transition). |

**Transition**: `{"type": "cut" | "dissolve" | "dip", "duration": seconds, "color": "#rrggbb"}`

- `cut`: instant.
- `dissolve`: a true crossfade. The incoming item fades in over the outgoing one, which stays fully opaque underneath until the dissolve completes. For videos, the dissolve starts `duration` seconds before the outgoing video ends, so nothing freezes. Audio crossfades over the same time.
- `dip`: fade out to `color`, then fade in to the next item (half the duration each). The next video starts at the midpoint.

Shorthand: a transition can also be given as just the type string (`"dissolve"`), using a duration of 1 s and black.

Which transition is used:

1. For a manual command (`play`, `next`, `previous`, `stop`), the `transition` passed with the request.
2. Otherwise the target item's `transition`.
3. Otherwise the playlist's `transition`.
4. Otherwise the global `default_transition`.

For `end_action: goto`, the goto's own `transition` comes first in this order.

## Status

### `GET /api/status`

```json
{
  "state": "playing",
  "playlist": {"id": "lobby", "name": "Lobby Loop"},
  "index": 0,
  "item": {"uid": "a1b2c3d4", "media": "welcome.mp4", "kind": "video"},
  "position": 12.4,
  "duration": 30.0,
  "next": {"playlist": "lobby", "index": 1, "media": "menu.png",
           "transition": {"type": "dissolve", "duration": 1.0, "color": "#000000"}},
  "loading": false,
  "transitioning": false,
  "loop_item": false,
  "last_error": null,
  "last_error_at": null,
  "output": {"backend": "gstreamer", "render_size": [1920, 1080], "fps": 30,
             "display": {"connector": "HDMI-A-1", "mode": "1920x1080", "connected": true},
             "frames_rendered": 5402, "frames_dropped": 0, "audio_device": "default:CARD=vc4hdmi"}
}
```

`loop_item` is `true` while loop-item mode is on (see Transport).

`state` is one of `starting`, `playing`, `paused`, `held` (ended with `hold`), `stopped`, `dormant` (no display is connected — see below), `error` (the renderer failed and is restarting).

`last_error` is the most recent error message, and `last_error_at` is when it happened (epoch seconds), or both `null` if there has not been one. They are **history and are never cleared**: a player that recovered hours ago still reports the error it recovered from. Use `state` to tell whether anything is wrong right now, and `last_error_at` to judge how old the message is.

`display_connected` is `true`, `false`, or `null` when the renderer has no concept of a display (the simulator). It is present in **every** state, including those where `output` is `null`, so a monitor can watch this one field rather than parsing `last_error`.

### Running without a display

The renderer needs a connected display to start. If the player restarts while the display is off or unplugged — after a power cut, say — it enters `dormant` rather than `error`: it retries every 30 seconds, reports `display_connected: false`, and resumes whatever it was playing as soon as a display appears. No intervention is needed.

A display switched off *during* playback does not interrupt anything; the player keeps running and `display_connected` becomes `false` until it returns.

To alert on a missing display:

```
GET /api/status  ->  .display_connected == false
```
`next` is `{"action": "stop"}` or `{"action": "hold"}` when the playlist won't continue.

### `GET /api/ws` (WebSocket)

Pushes JSON messages:

- `{"type": "status", "status": {...}}`: on every change, and every second while playing.
- `{"type": "playlist"}`, `{"type": "media"}`, `{"type": "settings"}`: something changed; re-fetch if you care.

## Transport

All transport endpoints return the new status. The optional `transition` parameter overrides the transition for that one cue. In a JSON body it's a transition object. In a query string it's `transition=<type>&duration=<s>&color=%23rrggbb`.

| Method & path | Parameters | Effect |
|---|---|---|
| `POST /api/play` | `playlist`, `index` (default 0), `transition` | Start a playlist. With no `playlist`: resume if paused, else restart the last (or default) playlist. |
| `POST /api/playlists/{id}/play` | `index`, `transition` | Start playlist `{id}`. |
| `POST /api/pause` | | Pause (freezes the current frame). |
| `POST /api/resume` | | Resume after pause. |
| `POST /api/toggle` | | Pause/resume. |
| `POST /api/stop` | `transition` | Transition to the background color and stop. |
| `POST /api/next` | `transition` | Next item. At the end of the playlist, follows the playlist's end action. |
| `POST /api/previous` | `transition` | Previous item (wraps to the last item). |
| `POST /api/loop-item` | `enabled`: `true`, `false` or `"toggle"` (default) | Loop-item mode: repeat the current item instead of advancing. Videos restart with a cut; images stay up. Next/previous still work. Handy while adjusting an item's position. |

Examples:

```bash
curl -X POST http://pipair.local/api/playlists/lobby/play
curl -X POST http://pipair.local/api/next -H 'Content-Type: application/json' \
     -d '{"transition": {"type": "dip", "duration": 2, "color": "#ffffff"}}'
curl 'http://pipair.local/api/play?playlist=show&index=3&transition=cut'
curl -X POST http://pipair.local/api/stop
```

## Playlists

| Method & path | Body | Result |
|---|---|---|
| `GET /api/playlists` | | Array of playlists. Items include the extra read-only fields `kind`, `missing` and `media_duration`. |
| `GET /api/playlists/{id}` | | One playlist. |
| `POST /api/playlists` | playlist object (needs `id`) | Create. `201`, or `400` if the id is taken or invalid. |
| `PUT /api/playlists/{id}` | full playlist object | Replace. Include `id` to rename. |
| `PATCH /api/playlists/{id}` | any top-level fields | Update only the given fields (`items` is replaced as a whole). |
| `PATCH /api/playlists/{id}/items/{uid}` | any item fields | Update one item, for example `{"offset_x": 25, "offset_y": -10}`. Returns the playlist. |
| `DELETE /api/playlists/{id}` | | Delete. |

Edits apply live: if you change the playing playlist, playback continues from the current item under the new rules. Position offsets move the on-screen item immediately.

Nudging an item's position while it's on screen:

```bash
curl -X POST http://pipair.local/api/playlists/lobby/play -d '{"index": 1, "transition": "cut"}' -H 'Content-Type: application/json'
curl -X POST 'http://pipair.local/api/loop-item?enabled=true'
curl -X POST http://pipair.local/api/pause                  # optional: freeze a video frame
curl -X PATCH http://pipair.local/api/playlists/lobby/items/e5f6a7b8 \
     -H 'Content-Type: application/json' -d '{"offset_x": -40, "offset_y": 12}'
curl -X POST 'http://pipair.local/api/loop-item?enabled=false'
```

```bash
curl -X POST http://pipair.local/api/playlists -H 'Content-Type: application/json' -d '{
  "id": "lobby", "name": "Lobby Loop",
  "items": [{"media": "welcome.mp4"}, {"media": "menu.png", "duration": 8}],
  "transition": "dissolve", "end_action": "loop"}'
```

## Media

| Method & path | Notes |
|---|---|
| `GET /api/media` | Array of `{name, kind, size, mtime, info, warnings, used_by}`. `info` holds probe results (`duration`, `width`, `height`, `fps`, `video_codec`, `audio_codec`) once available. `warnings` flags files the hardware can't decode smoothly. |
| `GET /api/media/{name}` | One file (probes it if needed). |
| `GET /api/media/{name}/file` | Download the original. |
| `GET /api/media/{name}/thumb` | JPEG thumbnail (images only). |
| `POST /api/media` | `multipart/form-data` upload. One or more file fields; any field name. Returns `{"uploaded": [names]}`. Name clashes get ` (2)` appended. |
| `DELETE /api/media/{name}` | `409` if playlists use it; add `?force=1` to delete it and remove it from those playlists. |

```bash
curl -F file=@welcome.mp4 -F file=@menu.png http://pipair.local/api/media
```

Files can also be copied straight into the media directory (`/var/lib/pipair/media` on an installed Pi) with `scp` or `rsync`. They appear automatically.

Supported extensions: videos `.mp4 .m4v .mov .mkv .webm .avi .ts .mpg .mpeg`; images `.jpg .jpeg .png .bmp .gif .webp .tif .tiff`. Only files the Pi can decode in hardware play smoothly. See the README for encoding guidance.

## Settings

### `GET /api/settings`

### `PATCH /api/settings` (`PUT` is accepted as an alias)

Send only the fields to change; nested objects are merged.

```json
{
  "default_playlist": "lobby",
  "default_transition": {"type": "dissolve", "duration": 1.0, "color": "#000000"},
  "default_image_duration": 10,
  "default_fit": "contain",
  "background_color": "#000000",
  "output": {"mode": "auto"},
  "audio": {"enabled": true, "device": "auto", "volume": 100},
  "gpi": {"enabled": true, "inputs": []},
  "link": {"av_offset_ms": 0}
}
```

| Setting | Notes |
|---|---|
| `default_playlist` | Played automatically at boot (`null` = start idle). |
| `background_color` | Shown when stopped and behind letterboxed content. Applies live. |
| `output.mode` | Display mode: `auto`, `WIDTHxHEIGHT` or `WIDTHxHEIGHT@HZ`. `auto` uses the display's preferred resolution; on a Pi 3 it prefers 30 Hz so two 1080p videos can be on screen at once for dissolves. `/api/status` → `output.display.modes` lists what the display supports. |
| `output.render_size`, `output.fps`, `output.rotation` | Only used by the optional GL-compositing renderer (`--backend gl`). |
| `audio.device` | `auto` or an ALSA device string such as `default:CARD=Headphones`. On the controller `auto` is a USB or HAT sound card if one is fitted, otherwise the 3.5 mm jack, otherwise HDMI. |
| `link.av_offset_ms` | Moves the picture against the sound, in milliseconds (-2000 to 2000). Positive shows the picture later. Changing it restarts the current item. |
| `audio.volume` | 0-100; applies live. |

Changing `output.*`, `audio.enabled` or `audio.device` restarts the renderer (about 1-2 s of black), then resumes the current item.

On the Pi's default renderer, the `output` object in `/api/status` also includes `frames_presented` (display updates), `commit_failures`, `display.modes`, `display.forceable` (standard modes you can force even though the display doesn't list them), and `display.forced`. When `video_overlap` is `false`, the display can't show two videos at once; `limits` then describes the consequence: video-to-video dissolves freeze the outgoing clip.

## The head link

The API is served by the controller. The picture is shown by the head, which the controller drives over Bluetooth LE; everything in `output` except `audio_device` describes the head's display.

`/api/status` → `output.link` (also `/api/system` → `link`):

```json
{
  "transport": "ble", "connected": true, "synced": true, "ready": true,
  "state": "connected", "device": "pipair-head", "address": "B8:27:EB:12:34:56",
  "rtt_ms": 16.2, "rtt_median_ms": 23.9, "last_error": null,
  "head": {"name": "pipair-head", "version": "0.1.0", "ok": true, "no_display": false,
           "media_files": 4, "health": {"temperature_c": 51.2}}
}
```

| Field | Meaning |
|---|---|
| `connected` | The Bluetooth connection is up. |
| `synced` | The controller has measured the difference between the two devices' clocks. |
| `ready` | Connected, synced, and the head's renderer is running. Only then is there a picture. Watch this one field to monitor the head. |
| `state` | `searching`, `connecting`, `connected` or `error` (see `last_error`). |
| `rtt_ms`, `rtt_median_ms` | Best and typical ping round trip over the last few seconds. |
| `head.ok` | `false` while the head's renderer is down; `head.no_display` says whether that is for want of a display. |

While the head is away the controller keeps playing sound and the head keeps showing (and looping) the clip it had. When the head becomes `ready` the current item restarts on both, so they start together.

### Pairing

A controller drives one head: the one it has been paired with. `GET /api/link` returns the link object above plus:

```json
{
  "pair_state": "unpaired",
  "reason": null,
  "paired": null,
  "pairing": null,
  "heads": [{"address": "B8:27:EB:12:34:56", "name": "pipair-head", "rssi": -52, "connected": false}]
}
```

| Field | Meaning |
|---|---|
| `pair_state` | `unpaired`, `pairing` (connecting to the chosen head), `confirm` (waiting for the user to compare codes), `searching`, `authenticating`, `ready`, or `rejected` (the paired head turned this controller away; see `reason`). |
| `paired` | `{"name", "address"}` of the paired head, or `null`. |
| `pairing` | `{"address", "name", "code"}` while a pairing is under way. `code` is the 4 digits the head should be showing; it is `null` until the head has answered. |
| `heads` | PiPair heads seen recently, strongest signal first. Not refreshed while a head is connected. |

| Method & path | Body | Effect |
|---|---|---|
| `POST /api/link/pair` | `{"address": "..."}` | Start pairing with that head. 400 if a head is already paired, 404 if the address is not in `heads`. |
| `POST /api/link/confirm` | | The codes match: finish pairing. Only confirm after a person has compared the code with the head's display; confirming blindly gives up the protection the code provides. |
| `POST /api/link/cancel` | | Abandon the pairing. |
| `POST /api/link/unpair` | | Forget the paired head, and release it if it is connected. |

All four return the same object as `GET /api/link`. The WebSocket sends `{"type": "link"}` whenever any of it changes.

Media files must be on both devices under the same name. `GET /api/media` adds a warning to each file the head lacks, and playlist items carry `head_warning` (a string, or `null`).

## GPI triggers

Contact closures on GPIO pins that fire actions. The inputs are stored in settings (`gpi`); these endpoints add live state.

### `GET /api/gpi`

```json
{
  "enabled": true,
  "available": true,
  "chip": "/dev/gpiochip0",
  "error": null,
  "inputs": [
    {"id": "b1", "name": "Lobby button", "pin": 17, "header_pin": 11, "fire_on": "close",
     "pull": "up", "active_low": true, "debounce_ms": 20, "holdoff_ms": 300,
     "action": {"type": "play", "playlist": "lobby", "index": 0, "transition": null},
     "state": "open", "error": null, "count": 3, "last": 1789760000.1, "last_error": null}
  ],
  "pins": [{"gpio": 17, "header_pin": 11, "note": null}],
  "ground_pins": [6, 9, 14, 20, 25, 30, 34, 39]
}
```

`state` is the live contact state (`open`/`closed`). `count`, `last` and `last_error` describe firings since PiPair started. `available: false` with an `error` means GPIO can't be used (for example, not on a Pi).

### `PUT /api/gpi`

Replaces the configuration: `{"enabled": true, "inputs": [...]}`. Returns the same shape as `GET`.

| Input field | Meaning |
|---|---|
| `id` | Stable id (generated if omitted). |
| `name` | Label. |
| `pin` | BCM GPIO number, 0-27 (not the header pin number). |
| `fire_on` | `close` (default), `open` or `both`. |
| `pull` | `up` (default: contact to GND), `down`, or `none`. |
| `active_low` | Whether a low pin counts as "closed". Defaults to `true`, or `false` with `pull: down`. |
| `debounce_ms` | Kernel debounce, 0-1000 (default 20). |
| `holdoff_ms` | Ignore re-triggers for this long after firing, 0-60000 (default 300). |
| `action.type` | `play`, `next`, `previous`, `stop`, `pause`, `resume`, `toggle` or `loop_item`. |
| `action.playlist`, `action.index` | For `play`. |
| `action.transition` | For `play`, `next`, `previous` and `stop` (`null` = the usual transition). |
| `action.mode` | For `loop_item`: `toggle` (default), `on` or `off`. |

Inputs sharing a pin must use the same `pull`, `active_low` and `debounce_ms`.

### `POST /api/gpi/{id}/fire`

Runs the input's action now, as if it had been triggered. This is useful for testing, and GET works too.

The WebSocket also sends `{"type": "gpi", "pin": 17, "event": "close"}` on every edge, and `{"type": "gpi", "input": "b1", "fired": true, "source": "GPIO 17 close"}` when an input fires.

## System

| Method & path | Notes |
|---|---|
| `GET /api/system` | Version, Pi model, HDMI outputs and modes, temperature, throttling / under-voltage flags, recent errors, renderer info. |
| `POST /api/system/restart-renderer` | Rebuild the video pipeline and resume the current item. |
| `GET /api/preview.jpg` | A small JPEG of what is currently on screen. |
| `GET /api/docs` | This document (Markdown). |
