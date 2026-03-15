"""Custom exception hierarchy for Sentinel."""


class SentinelError(Exception):
    """Base exception for all Sentinel errors."""
    pass


# Keep AQEError as alias for backward compatibility
AQEError = SentinelError


class ConfigurationError(SentinelError):
    """Missing or invalid configuration."""
    pass


class DiscoveryError(SentinelError):
    """ERC-8004 event query or agent lookup failure."""
    pass


class IdentityVerificationError(SentinelError):
    """Agent manifest fetch or validation failure."""
    pass


class LivenessCheckError(SentinelError):
    """Endpoint liveness check failure."""
    pass


class OnchainAnalysisError(SentinelError):
    """On-chain wallet analysis failure."""
    pass


class VeniceError(SentinelError):
    """Venice API call failure."""
    pass


class VeniceParseError(VeniceError):
    """Venice returned unparseable output after all fallback layers."""
    pass


class BlockchainError(SentinelError):
    """web3.py transaction failure."""
    pass


class EASError(BlockchainError):
    """EAS attestation failure."""
    pass


class BudgetExhaustedError(SentinelError):
    """Tool call budget (15) exceeded."""
    pass


class HumanReviewTimeoutError(SentinelError):
    """Human did not respond within timeout window."""
    pass
