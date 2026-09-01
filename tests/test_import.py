import pollard
from pollard import DuplicateRecording, Store


def test_version() -> None:
    assert pollard.__version__ == "1.6.0"


def test_store_protocol_is_public() -> None:
    assert pollard.Store is Store
    assert "Store" in pollard.__all__


def test_duplicate_recording_is_public() -> None:
    assert pollard.DuplicateRecording is DuplicateRecording
    assert "DuplicateRecording" in pollard.__all__


def test_dir_adds_lazy_public_api_without_hiding_existing_names() -> None:
    namespace_before = dict(pollard.__dict__)
    names = set(dir(pollard))

    assert set(pollard.__all__) <= names
    assert set(namespace_before) <= names
    assert pollard.__dict__ == namespace_before
