import os

# Tests must never touch the keyring of the person running them.
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"

# Nor their clipboard or notifications: with no display or session bus, the clipboard watcher
# stays off. Tests that need one start Xvfb, a headless compositor, or a private bus.
for name in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "DBUS_SESSION_BUS_ADDRESS"):
    os.environ.pop(name, None)
