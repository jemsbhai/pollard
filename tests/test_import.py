import pollard
from pollard import Store


def test_version() -> None:
    assert pollard.__version__ == "1.1.1"


def test_store_protocol_is_public() -> None:
    assert pollard.Store is Store
    assert "Store" in pollard.__all__
