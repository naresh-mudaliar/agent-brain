"""Memory archive compression and Fernet AES encryption module for agent-brain."""

from .s3_sync import (
    DEFAULT_ARCHIVE_FILE,
    DEFAULT_DB_FILE,
    DEFAULT_SOURCE_DIR,
    ENV_SECRET_KEY,
    DecryptionError,
    EncryptionError,
    SyncError,
    export_memory_archive,
    generate_encryption_key,
    hydrate_database_on_startup,
    import_memory_archive,
)

__all__ = [
    "export_memory_archive",
    "import_memory_archive",
    "hydrate_database_on_startup",
    "generate_encryption_key",
    "SyncError",
    "EncryptionError",
    "DecryptionError",
    "ENV_SECRET_KEY",
    "DEFAULT_ARCHIVE_FILE",
    "DEFAULT_SOURCE_DIR",
    "DEFAULT_DB_FILE",
]
