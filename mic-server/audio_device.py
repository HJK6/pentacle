"""Resolve an input device for the local audio adapter."""

import os
import sounddevice as sd

DISALLOWED_INPUT_NAMES = (
    "Synthetic Loopback Microphone",
    "Synthetic Loopback Audio",
)


def refresh_audio_backend():
    """Re-enumerate the CoreAudio/PortAudio device list from scratch.

    PortAudio caches the device object table (CoreAudio AudioObjectIDs) at
    Pa_Initialize, which for the long-running mic-server happens once at process
    start. When the audio topology changes afterwards -- the USB mic is
    replugged, the default input is switched, the login session relogs, or
    coreaudiod restarts -- those cached IDs go stale. Opening an InputStream
    then fails against dead objects: AUHAL logs ``err='!obj'``
    (kAudioHardwareBadObjectError) and ``err='-10851'``
    (kAudioUnitErr_InvalidPropertyValue), which sounddevice surfaces as
    ``PaErrorCode -9986`` (paInternalError). Note that ``sd.query_devices()``
    keeps returning the stale (but plausible-looking) list, so device
    *resolution* still succeeds while the *open* fails.

    Terminating and re-initializing PortAudio rebuilds the table against the
    current HAL state. Best-effort: on failure we log and fall through, letting
    resolve/open surface any genuine device error with a clear message.
    """
    try:
        sd._terminate()
        sd._initialize()
    except Exception as exc:  # pragma: no cover - defensive; probe-verified path
        print(f"[mic] audio backend refresh failed: {exc}")


def resolve_mic_device(preferred_device_name=None):
    """Return structured device selection details for health reporting."""
    preferred = (
        os.environ.get("MIC_DEVICE_NAME", "").strip()
        if preferred_device_name is None
        else preferred_device_name.strip()
    )
    devices = sd.query_devices()
    result = {
        "index": None,
        "selected_device": None,
        "preferred_device_name": preferred or None,
        "preferred_present": False,
        "disallowed_device": False,
    }

    if preferred:
        for i, dev in enumerate(devices):
            if dev.get("max_input_channels", 0) <= 0:
                continue
            if preferred.lower() in dev.get("name", "").lower():
                result["preferred_present"] = True
                return _selection_result(result, i, dev)
        return result

    default_idx = sd.default.device[0]
    if default_idx is not None and default_idx >= 0:
        dev = sd.query_devices(default_idx)
        if dev.get("max_input_channels", 0) > 0:
            return _selection_result(result, default_idx, dev)

    for i, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) > 0:
            candidate = _selection_result(result, i, dev)
            if not candidate["disallowed_device"]:
                return candidate
            return candidate

    return result


def find_mic_index():
    """Return the selected input device index, raising on unsafe selections."""
    selection = resolve_mic_device()
    if selection["index"] is not None and not selection["disallowed_device"]:
        print(f"[mic] Using device: {selection['selected_device']}")
        return selection["index"]

    preferred = selection["preferred_device_name"]
    if preferred and not selection["preferred_present"]:
        raise RuntimeError(_missing_preferred_message(preferred))
    if selection["disallowed_device"]:
        raise RuntimeError(f"Refusing disallowed input device: {selection['selected_device']}")
    raise RuntimeError("No input audio devices found")


def _selection_result(base, index, device):
    result = dict(base)
    name = device.get("name", "")
    disallowed = _is_disallowed_input(name)
    result.update({
        "index": None if disallowed else index,
        "selected_device": name,
        "disallowed_device": disallowed,
    })
    return result


def _is_disallowed_input(name):
    folded = name.lower()
    return any(blocked.lower() in folded for blocked in DISALLOWED_INPUT_NAMES)


def _missing_preferred_message(preferred):
    devices = sd.query_devices()
    available = [
        f"  {i}: {d['name']} ({d['max_input_channels']} in)"
        for i, d in enumerate(devices) if d["max_input_channels"] > 0
    ]
    return f"Mic '{preferred}' not found. Available input devices:\n" + "\n".join(available)


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
