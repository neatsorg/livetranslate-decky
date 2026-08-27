"""One-shot smoke test for dxcam (DXGI Desktop Duplication) screen capture on
Windows, targeting the internal/primary LCD (device_idx=0, output_idx=0) -
the distribution target, not the external monitors this dev laptop also has.

Includes a warm-up: the first grab() right after a duplicator is created can
come back black/stale (a known DXGI Desktop Duplication quirk - no frame has
actually been produced yet), so this discards a couple of early frames
before treating a black result as a real failure.
"""
import sys
import time


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "capture_test_out.png"

    print(f"python: {sys.version}")

    import dxcam
    import numpy as np
    from PIL import Image

    camera = dxcam.create(device_idx=0, output_idx=0)
    print(f"camera: {camera}")

    frame = None
    for attempt in range(5):
        frame = camera.grab()
        if frame is not None and float(np.mean(frame)) > 0.5:
            print(f"attempt {attempt}: got non-black frame")
            break
        print(f"attempt {attempt}: frame={'None' if frame is None else 'black/near-black'}, retrying")
        time.sleep(0.1)

    if frame is None:
        print("FAILED: grab() returned None on every attempt")
        sys.exit(1)

    mean = float(np.mean(frame))
    print(f"frame shape={frame.shape} dtype={frame.dtype} mean={mean:.2f}")
    img = Image.fromarray(frame)
    img.save(out_path)
    print(f"OK: saved {out_path} ({img.size[0]}x{img.size[1]})")


if __name__ == "__main__":
    main()
