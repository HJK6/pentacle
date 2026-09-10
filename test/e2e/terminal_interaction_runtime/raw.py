"""Owned raw stdin collector. Its path and label come from the harness."""
import os
import sys
import tty

tty.setraw(sys.stdin.fileno())
for index in range(200):
    os.write(1, ("HISTORY_SENTINEL_%03d sample text\r\n" % index).encode())
os.write(1, b"\x1b[?2004hCURRENT_SELECTION_SENTINEL\r\nRAW READY\r\n")
while True:
    data = os.read(0, 65536)
    if not data:
        break
    with open(sys.argv[1], "ab") as output:
        output.write(data)
