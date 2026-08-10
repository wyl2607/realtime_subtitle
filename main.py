"""Application entrypoint (kept at repo root for Windows shortcuts).

Heavy imports and torch-before-PyQt ordering live in realtime_subtitle.app.
"""
from realtime_subtitle.app import main

if __name__ == "__main__":
    main()
