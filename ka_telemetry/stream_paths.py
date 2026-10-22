"""stream_paths — where the telemetry stream lives when no directory is named.

The receiver writes the stream and the reader reads it, and with no directory given both must
mean the same one. This is that one place. The value is the raw string, with its `~` unexpanded:
expanding it reaches into the environment, and the two composition roots (`receiver.build_receiver`
and `stream_usage.build_reader`) are the only places that do, so importing this module needs no
usable home directory.
"""

from __future__ import annotations

DEFAULT_STREAM_DIRECTORY = "~/.ka-telemetry"
