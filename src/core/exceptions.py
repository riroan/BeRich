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
    """Order submission/execution errors"""
    pass


class RiskLimitError(TradingBotError):
    """Risk limit exceeded"""
    pass


class ConfigurationError(TradingBotError):
    """Configuration errors"""
    pass


class DataError(TradingBotError):
    """Data-related errors"""
    pass
