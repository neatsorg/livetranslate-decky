"""Diagnostic dump for dxcam device/output enumeration + a grab from every
(device, output) pair, to find which one actually carries live pixels on a
multi-monitor / hybrid-GPU laptop where the default (0, 0) pick returned an
all-black frame."""
import sys

import dxcam
import numpy as np
from PIL import Image

print("=== device_info ===")
print(dxcam.device_info())
print("=== output_info ===")
print(dxcam.output_info())

out_dir = sys.argv[1] if len(sys.argv) > 1 else "."

# device_info()/output_info() are text dumps; parse device/output counts by
# just trying indices until creation fails, rather than parsing the string.
for device_idx in range(4):
    for output_idx in range(4):
        try:
            cam = dxcam.create(device_idx=device_idx, output_idx=output_idx)
        except Exception as exc:
            continue
        try:
            frame = cam.grab()
        except Exception as exc:
            print(f"device={device_idx} output={output_idx}: grab() raised {exc!r}")
            continue
        if frame is None:
            print(f"device={device_idx} output={output_idx}: grab() -> None")
            continue
        mean = float(np.mean(frame))
        path = f"{out_dir}\\diag_d{device_idx}_o{output_idx}.png"
        Image.fromarray(frame).save(path)
        print(f"device={device_idx} output={output_idx}: shape={frame.shape} mean={mean:.2f} -> {path}")
        del cam

print("done")
