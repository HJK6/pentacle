"""Cross-platform clipboard copy."""

import sys
import subprocess
import shutil


def copy_to_clipboard(text):
    """Copy text to the system clipboard. Works on macOS, Windows, and Linux."""
    if sys.platform == "darwin":
        proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        proc.communicate(text.encode("utf-8"))
    elif sys.platform == "win32":
        proc = subprocess.Popen(["clip.exe"], stdin=subprocess.PIPE)
        proc.communicate(text.encode("utf-16-le"))
    else:
        # Linux / WSL — try clip.exe (WSL), then xclip, then xsel
        if shutil.which("clip.exe"):
            proc = subprocess.Popen(["clip.exe"], stdin=subprocess.PIPE)
            proc.communicate(text.encode("utf-8"))
        elif shutil.which("xclip"):
            proc = subprocess.Popen(
                ["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE
            )
            proc.communicate(text.encode("utf-8"))
        elif shutil.which("xsel"):
            proc = subprocess.Popen(
                ["xsel", "--clipboard", "--input"], stdin=subprocess.PIPE
            )
            proc.communicate(text.encode("utf-8"))
        else:
            print("[clipboard] No clipboard command found (tried clip.exe, xclip, xsel)")
