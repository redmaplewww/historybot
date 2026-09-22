import pytest

from historybot.domain import Polarity
from historybot.graph import normalize_polarity


def test_model_polarity_alias_is_normalized_without_changing_semantics():
    assert normalize_polarity("supported") is Polarity.SUPPORTS
    assert normalize_polarity("支持") is Polarity.SUPPORTS
    assert normalize_polarity("positive") is Polarity.SUPPORTS
    assert normalize_polarity("contradicts") is Polarity.REFUTES
    with pytest.raises(ValueError):
        normalize_polarity("invented_status")
