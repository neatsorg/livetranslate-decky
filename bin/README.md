# playtranslate-deck

Steam Deck game mode PoC for receiving Gamescope frames through PipeWire,
cropping a region with GStreamer, and counting frames in Python through
`appsink`.

This intentionally does not do OCR, translation, networking, or overlay output
yet. The first goal is to verify that a small program can receive live
crop-ready frames from Gamescope.

## Assumptions Already Confirmed

- PipeWire node: `node.name=gamescope`
- PipeWire media class: `media.class=Video/Source`
- GStreamer can read the source with `pipewiresrc target-object=<object.serial>`
- The current tested capture format is `1920x1080 BGRx`
- `videocrop` can crop the live stream

Do not hardcode the old `target-object=96` value unless you are deliberately
testing one session. The serial can change.

## Steam Deck Dependencies

Check the basics:

```bash
which python3
which gst-launch-1.0
which gst-inspect-1.0
which pw-dump
python3 -c 'import gi; gi.require_version("Gst", "1.0"); from gi.repository import Gst; print("OK")'
gst-inspect-1.0 pipewiresrc >/dev/null && echo pipewiresrc OK
gst-inspect-1.0 videocrop >/dev/null && echo videocrop OK
gst-inspect-1.0 appsink >/dev/null && echo appsink OK
```

## Run

From this directory on the Steam Deck:

```bash
python3 capture.py --list-node
python3 capture.py --print-pipeline --duration 10
```

Expected output includes the detected Gamescope node, the appsink caps, and
frame/FPS counters:

```text
Using PipeWire node: {'id': 93, 'object_serial': 96, ...}
appsink caps: video/x-raw, format=(string)BGRx, ...
frames=60 fps=60.0 avg_fps=60.0
```

If auto detection fails but you know the current serial:

```bash
python3 capture.py --target-object 96 --duration 10
```

If the installed `pipewiresrc` behaves better with node id:

```bash
python3 capture.py --path 93 --duration 10
```

## Crop Settings

`config.json` currently crops a subtitle-like ROI near the bottom of a
1920x1080 source:

```json
"roi": {
  "x": 200,
  "y": 750,
  "width": 1520,
  "height": 250
}
```

The program translates ROI into `videocrop` margins. For the current default,
that becomes:

```text
videocrop left=200 right=200 top=750 bottom=80
```

To test a raw crop instead, set `crop` to explicit margins.

## Next Step

After stable FPS is confirmed, enable the lightweight sampled frame-diff stage:

```bash
python3 capture.py --diff --duration 20
```

It checks sampled frames at about 20Hz and prints `change ...` lines when the
cropped region changes enough. Continuous motion is edge-triggered and debounced,
so it should not print a new event until the ROI becomes stable again. The
report line also includes suppressed changes and whether the detector is armed:

```text
frames=120 fps=60.0 avg_fps=59.9 change_ratio=0.0012 change_events=0 suppressed=0 armed=1
change frame=188 ratio=0.0341 events=1
```

Tune sensitivity with:

```bash
python3 capture.py --diff --diff-threshold 0.01 --diff-sample-stride 128 --duration 20
```

Lower threshold is more sensitive. Lower sample stride reads more bytes and is
more accurate, but costs more CPU.

If a moving game scene triggers too many events, crop a smaller subtitle ROI or
increase the debounce:

```bash
python3 capture.py --diff --diff-threshold 0.05 --diff-cooldown-frames 60 --duration 20
```

If the detector never re-arms because the ROI background is always moving, crop
a tighter text-only region or raise the reset threshold:

```bash
python3 capture.py --diff --diff-threshold 0.05 --diff-reset-threshold 0.03 --duration 20
```

## Stall Watchdog

gamescope's PipeWire source has been observed to silently stop delivering new
frame content (logged on the Deck as repeated `pipewire: warning: out of
buffers` in `journalctl`, outside this script's own log) while `frames`/`fps`
keep incrementing normally, so nothing here looks wrong at a glance. When
`--diff` is on, `capture.py` now rebuilds its capture pipeline automatically
if no change event has fired for `--stall-timeout-s` seconds (default 90):

```bash
python3 capture.py --diff --stall-timeout-s 60 --save-settled --save-dir out
```

Use `--stall-timeout-s 0` to disable. GStreamer `WARNING` bus messages are
also printed now (previously only `ERROR`/`EOS` were surfaced), so a
recurrence should show up directly in this script's own log instead of only
in `journalctl`.

One confirmed trigger for the above: the Deck suspending and resuming mid-session.
`journalctl -k` shows the whole GPU/PipeWire/USB stack reinitializing on resume
(`ACPI: PM: Waking up from system sleep state S3` followed by amdgpu/PSP/SMU
resume messages and a burst of `out of buffers`), and gamescope's PipeWire
source doesn't reliably recover from that on its own. `capture.py` detects
this directly - if wall-clock time jumps forward by `--resume-gap-s` (default
5s) between main-loop iterations, that's a suspend/resume, and it rebuilds the
pipeline after `--resume-grace-s` (default 3s, to give gamescope a moment to
stabilize first) instead of waiting on the slower stall watchdog above. Use
`--resume-gap-s 0` to disable.
