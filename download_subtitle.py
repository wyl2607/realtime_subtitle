"""Command-line entrypoint for the offline download/subtitle pipeline."""

from realtime_subtitle.offline import main


if __name__ == "__main__":
    raise SystemExit(main())
