"""Provision captured voice clips; synthesis only, never playback."""
import argparse
from pathlib import Path
from resident_speaker import ResidentSpeaker, NullSink, render_clips
from voice_rules import Rules


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--rules')
    args = parser.parse_args()
    speaker = ResidentSpeaker(sink=NullSink())
    try:
        speaker.start()
        render_clips(speaker, Path(args.output), Rules(args.rules).snapshot()['clips'])
    finally:
        speaker.close()


if __name__ == '__main__':
    main()
