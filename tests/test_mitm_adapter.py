import mitmproxy.version
import pytest
from mitmproxy.proxy.layers.http import HttpStream

from daemon.capture import mitm_adapter


def test_pin_matches_the_installed_version() -> None:
    assert mitmproxy.version.VERSION == mitm_adapter.SUPPORTED_VERSION
    mitm_adapter.check_version()


def test_install_refuses_any_other_version(monkeypatch: pytest.MonkeyPatch) -> None:
    original = HttpStream.start_response_stream
    monkeypatch.setattr(mitmproxy.version, "VERSION", "12.3.2")
    with pytest.raises(RuntimeError, match=r"supports mitmproxy 12\.3\.1 only, found 12\.3\.2"):
        mitm_adapter.install()
    assert HttpStream.start_response_stream is original
