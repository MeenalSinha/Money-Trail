"""Exception hierarchy. Messages must never contain secrets."""


class MoneyTrailError(Exception):
    """Base class for all application errors."""


class ConfigurationError(MoneyTrailError):
    """Configuration is missing or invalid."""


class DatabaseConnectionError(MoneyTrailError):
    """FalkorDB could not be reached or rejected the connection."""


class SchemaError(MoneyTrailError):
    """Schema initialization or verification failed."""


class ValidationError(MoneyTrailError):
    """Input failed validation before being written to the graph."""


class GraphWriteError(MoneyTrailError):
    """A graph write did not complete as expected (database error or unmatched endpoints)."""


class DatasetConfigError(MoneyTrailError):
    """A dataset or generator configuration file is missing, malformed or invalid."""


class SourceFormatError(MoneyTrailError):
    """A source file cannot be read as the configured format (missing columns, size limits, encoding)."""


class QueryError(MoneyTrailError):
    """An investigation query was given invalid arguments or could not be completed."""
