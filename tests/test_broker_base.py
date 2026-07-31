from src.broker.base import Broker


def test_broker_protocol_importable():
    assert Broker is not None
