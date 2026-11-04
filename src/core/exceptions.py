class TradingBotError(Exception):
    """Base exception for trading bot"""
    pass


class BrokerError(TradingBotError):
    """Broker-related errors"""
    pass


class AuthenticationError(BrokerError):
    """Authentication failed"""
    pass


class OrderError(BrokerError):
    """Order submission/execution errors.

    ``code`` is the broker's error code when it gave one (KIS ``msg_cd``):
    a stable identity for the failure, unlike the message text.
    """

    def __init__(self, message: str = "", code: str | None = None):
        super().__init__(message)
        self.code = code


class RiskLimitError(TradingBotError):
    """Risk limit exceeded"""
    pass


class ConfigurationError(TradingBotError):
    """Configuration errors"""
    pass


class DataError(TradingBotError):
    """Data-related errors"""
    pass
