"""Auto-detect the active/default microphone.

Falls back to the system default input device. Set MIC_DEVICE_NAME env var
to override with a substring match (e.g. MIC_DEVICE_NAME="Samson Q9U").
"""

import os
import sounddevice as sd


def find_mic_index():
    """Return the device index for the preferred mic, or None for system default."""
    override = os.environ.get("MIC_DEVICE_NAME", "").strip()
    if not override:
        # Use system default — sounddevice handles this when device=None
        default_idx = sd.default.device[0]
        if default_idx is not None and default_idx >= 0:
            dev = sd.query_devices(default_idx)
            if dev["max_input_channels"] > 0:
                print(f"[mic] Using default input device: {dev['name']}")
                return default_idx
        # No valid default — pick first input device
        return _first_input_device()

    # Search by name substring
    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        if override.lower() in dev["name"].lower() and dev["max_input_channels"] > 0:
            print(f"[mic] Using device: {dev['name']}")
            return i

    available = [
        f"  {i}: {d['name']} ({d['max_input_channels']} in)"
        for i, d in enumerate(devices) if d["max_input_channels"] > 0
    ]
    raise RuntimeError(
        f"Mic '{override}' not found. Available input devices:\n" + "\n".join(available)
    )


def _first_input_device():
    """Return the index of the first device with input channels."""
    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            print(f"[mic] Using first available input: {dev['name']}")
            return i
    raise RuntimeError("No input audio devices found")


def list_devices():
    """Print all audio devices for debugging."""
    devices = sd.query_devices()
    print("Audio devices:")
    for i, dev in enumerate(devices):
        direction = ""
        if dev["max_input_channels"] > 0:
            direction += "IN"
        if dev["max_output_channels"] > 0:
            direction += "/OUT" if direction else "OUT"
        marker = " <-- default" if i == sd.default.device[0] else ""
        print(f"  {i}: {dev['name']} ({direction}){marker}")


if __name__ == "__main__":
    list_devices()
    idx = find_mic_index()
    print(f"\nSelected device index: {idx}")
