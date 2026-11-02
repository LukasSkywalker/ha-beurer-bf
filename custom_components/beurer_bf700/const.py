"""Constants for the Beurer BF700 integration."""

from datetime import timedelta

DOMAIN = "beurer_bf700"
MANUFACTURER = "Beurer"

# Re-read the scale at least this often while it is in range (users, status).
FULL_SYNC_INTERVAL = timedelta(hours=24)
# The advertised stamp is the time of the last weigh-in or clock sync, so the
# first stamp change after one of our own sessions is caused by our SET_TIME.
# Such a change inside this window after a session is treated as an echo.
ECHO_WINDOW = timedelta(minutes=10)
# Retry back-off after a failed connection (the scale may be asleep).
RETRY_MIN = timedelta(seconds=30)
RETRY_MAX = timedelta(minutes=30)
# Safety net against connect loops: after several syncs that found nothing
# new, stamp changes trigger a sync at most this often.
QUIET_SYNC_INTERVAL = timedelta(minutes=30)
QUIET_AFTER_EMPTY_SYNCS = 3

# The idle scale only now and then sends a connectable advertisement, so a
# connection can take a few 20 s attempts.
CONNECT_ATTEMPTS = 4

# How long to stay connected after reading the stored data, to catch a weigh-in.
LISTEN_IDLE_SECONDS = 10.0
LISTEN_MAX_SECONDS = 90.0

STORAGE_VERSION = 1
