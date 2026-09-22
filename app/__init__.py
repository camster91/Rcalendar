"""Rotman LSM Calendar — a Windows desktop calendar of LSM room bookings."""

# The same string as APP_VERSION in app.config, by import rather than by
# transcription: a hand-maintained second copy could drift and nothing would
# notice, since no code and no test reads both. app.config imports only the
# stdlib, so importing it from here cannot cycle.
from app.config import APP_VERSION as __version__