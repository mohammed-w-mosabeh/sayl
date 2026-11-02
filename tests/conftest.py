import os

# Tests must never touch the keyring of the person running them.
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
