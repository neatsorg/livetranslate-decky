# bin/ - PlayTranslate capture/OCR/translation engine

This directory is the Decky plugin's engine - see the [parent
README](../README.md) for the full feature set and layout. It's a set of
standalone Python scripts (not a package) launched as subprocesses by
`main.py`, and can also be run and tested directly on a Steam Deck as
described below.

This README documents `capture.py`, the legacy fixed-region capture path
that receives Gamescope frames through PipeWire and crops a region with
GStreamer. `capture_dynamic.py` (the default Dynamic Capture engine) reuses
the same PipeWire/GStreamer plumbing but adds full-frame text discovery,
OCR, and translation on top - see `PHASE_A_HANDOFF.md` in the repo root
for its design history.

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

Sometimes an in-process pipeline rebuild isn't enough by itself - gamescope's
own PipeWire producer can still be stuck even after this script cleanly
reconnects to it (confirmed live: a second concurrent PipeWire consumer of
the same gamescope node is one way to trigger this). If `--max-rebuilds-
before-reexec` (default 1) consecutive rebuilds happen with no real change
event landing in between, `capture.py` re-execs itself (`os.execvp`, same
PID) instead of rebuilding again - a much more thorough reset than
recreating the GStreamer pipeline object alone. Use `--max-rebuilds-before-
reexec 0` to never re-exec.
