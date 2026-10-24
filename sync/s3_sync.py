"""Archive encryption and synchronization utility for agent-brain.

Compresses the ./.agent-memory/ directory into a secure zip archive, encrypts it
using AES symmetric encryption (cryptography.fernet) using the secret key from
the environment variable AGENT_MEMORY_SECRET_KEY, and saves it as memory-seed.enc.

Also provides the reverse decryption and extraction routine to hydrate the local
KùzuDB database on startup.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import zipfile
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("agent_brain.sync")

ENV_SECRET_KEY = "AGENT_MEMORY_SECRET_KEY"
DEFAULT_SOURCE_DIR = "./.agent-memory"
DEFAULT_ARCHIVE_FILE = "memory-seed.enc"
DEFAULT_DB_FILE = "./.agent-memory/brain.db"


class SyncError(Exception):
    """Base exception for sync and archive operations."""


class EncryptionError(SyncError):
    """Raised when archive compression or encryption fails."""


class DecryptionError(SyncError):
    """Raised when archive decryption or extraction fails."""


def generate_encryption_key() -> str:
    """Generate a URL-safe base64-encoded 32-byte key for Fernet AES encryption."""
    return Fernet.generate_key().decode("utf-8")


def _get_fernet_instance(secret_key: Optional[str | bytes] = None) -> Fernet:
    """Retrieve and validate the Fernet cipher instance from arg or environment."""
    key = secret_key or os.environ.get(ENV_SECRET_KEY)
    if not key:
        raise ValueError(
            f"Encryption secret key is required. Pass 'secret_key' or set the "
            f"'{ENV_SECRET_KEY}' environment variable. You can generate a valid key using "
            f"'python -m sync.s3_sync generate-key'."
        )

    if isinstance(key, str):
        key = key.strip().encode("utf-8")

    try:
        return Fernet(key)
    except Exception as err:
        raise ValueError(
            f"Invalid Fernet key format: {err}. Key must be 32 URL-safe base64-encoded bytes."
        ) from err


def export_memory_archive(
    source_dir: str | Path = DEFAULT_SOURCE_DIR,
    output_file: str | Path = DEFAULT_ARCHIVE_FILE,
    secret_key: Optional[str | bytes] = None,
) -> Path:
    """Compress source directory into an in-memory zip archive and encrypt it to disk.

    Args:
        source_dir: Directory containing agent memory (e.g. ./.agent-memory).
        output_file: Destination path for encrypted archive (e.g. memory-seed.enc).
        secret_key: Fernet secret key or None to read from AGENT_MEMORY_SECRET_KEY.

    Returns:
        Path to the generated encrypted archive file.

    Raises:
        FileNotFoundError: If source_dir does not exist.
        EncryptionError: If archiving or encryption fails.
    """
    src_path = Path(source_dir).resolve()
    dest_path = Path(output_file).resolve()
    fernet = _get_fernet_instance(secret_key)

    if not src_path.exists():
        raise FileNotFoundError(f"Source directory '{src_path}' does not exist.")

    logger.info(f"Compressing '{src_path}' into in-memory zip archive...")

    try:
        # Create zip in-memory to prevent unencrypted zip file touching disk
        zip_buffer = io.BytesIO()
        file_count = 0
        with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(src_path):
                for file in files:
                    file_full = Path(root) / file
                    rel_name = file_full.relative_to(src_path)
                    zf.write(file_full, arcname=str(rel_name).replace("\\", "/"))
                    file_count += 1

        if file_count == 0:
            logger.warning(f"Source directory '{src_path}' is empty.")

        uncompressed_data = zip_buffer.getvalue()
        logger.info(
            f"Archived {file_count} files ({len(uncompressed_data)} bytes). Encrypting with Fernet AES..."
        )

        encrypted_data = fernet.encrypt(uncompressed_data)

        # Write encrypted payload to destination
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(encrypted_data)
        logger.info(
            f"Successfully saved encrypted archive to '{dest_path}' ({len(encrypted_data)} bytes)."
        )
        return dest_path

    except Exception as err:
        err_msg = f"Failed to export encrypted memory archive: {err}"
        logger.error(err_msg, exc_info=True)
        raise EncryptionError(err_msg) from err


def import_memory_archive(
    archive_file: str | Path = DEFAULT_ARCHIVE_FILE,
    target_dir: str | Path = DEFAULT_SOURCE_DIR,
    secret_key: Optional[str | bytes] = None,
) -> Path:
    """Decrypt an encrypted archive file and extract its contents into target directory.

    Args:
        archive_file: Path to encrypted archive (e.g. memory-seed.enc).
        target_dir: Target directory where memory files will be restored.
        secret_key: Fernet secret key or None to read from AGENT_MEMORY_SECRET_KEY.

    Returns:
        Path to restored target directory.

    Raises:
        FileNotFoundError: If archive_file does not exist.
        DecryptionError: If decryption or zip extraction fails (e.g. invalid key or corrupted data).
    """
    archive_path = Path(archive_file).resolve()
    target_path = Path(target_dir).resolve()
    fernet = _get_fernet_instance(secret_key)

    if not archive_path.exists():
        raise FileNotFoundError(f"Archive file '{archive_path}' does not exist.")

    logger.info(f"Reading encrypted archive from '{archive_path}'...")

    try:
        encrypted_data = archive_path.read_bytes()
    except Exception as err:
        raise DecryptionError(f"Could not read archive file '{archive_path}': {err}") from err

    try:
        decrypted_bytes = fernet.decrypt(encrypted_data)
    except InvalidToken as err:
        err_msg = (
            "Decryption failed: Invalid secret key or corrupted archive "
            f"'{archive_path}'."
        )
        logger.error(err_msg)
        raise DecryptionError(err_msg) from err
    except Exception as err:
        err_msg = f"Unexpected error during decryption: {err}"
        logger.error(err_msg)
        raise DecryptionError(err_msg) from err

    # Extract in-memory zip buffer
    try:
        target_path.mkdir(parents=True, exist_ok=True)
        zip_buffer = io.BytesIO(decrypted_bytes)
        file_count = 0

        with zipfile.ZipFile(zip_buffer, "r") as zf:
            for member in zf.infolist():
                # Protect against Zip Slip directory traversal vulnerability
                extracted_file_path = (target_path / member.filename).resolve()
                if not str(extracted_file_path).startswith(str(target_path)):
                    raise DecryptionError(
                        f"Unsafe path traversal detected in zip archive: '{member.filename}'"
                    )

            zf.extractall(target_path)
            file_count = len(zf.namelist())

        logger.info(
            f"Successfully decrypted and extracted {file_count} files into '{target_path}'."
        )
        return target_path

    except DecryptionError:
        raise
    except Exception as err:
        err_msg = f"Failed to extract decrypted archive contents: {err}"
        logger.error(err_msg, exc_info=True)
        raise DecryptionError(err_msg) from err


def hydrate_database_on_startup(
    archive_file: str | Path = DEFAULT_ARCHIVE_FILE,
    target_dir: str | Path = DEFAULT_SOURCE_DIR,
    secret_key: Optional[str | bytes] = None,
    force: bool = False,
) -> bool:
    """Hydrate the local KùzuDB database on application startup if needed.

    If target_dir / brain.db already exists and force is False, skips extraction.
    Otherwise, if archive_file exists, decrypts and restores memory.

    Args:
        archive_file: Path to memory-seed.enc.
        target_dir: Target directory (default: ./.agent-memory).
        secret_key: Fernet secret key or None.
        force: If True, overwrites existing database files.

    Returns:
        True if the database was hydrated from archive, False otherwise.
    """
    archive_path = Path(archive_file).resolve()
    target_path = Path(target_dir).resolve()
    db_file = target_path / "brain.db"

    if db_file.exists() and not force:
        logger.info(
            f"Local database '{db_file}' already exists. Skipping startup hydration."
        )
        return False

    if not archive_path.exists():
        logger.info(
            f"No memory seed archive found at '{archive_path}'. Starting with fresh memory."
        )
        return False

    logger.info(f"Hydrating local agent memory from '{archive_path}'...")
    import_memory_archive(
        archive_file=archive_path, target_dir=target_path, secret_key=secret_key
    )
    logger.info("Startup hydration complete.")
    return True


# S3 optional synchronization hooks
def upload_to_s3(
    file_path: str | Path,
    bucket_name: str,
    s3_key: Optional[str] = None,
) -> bool:
    """Optional helper to upload an encrypted archive to an AWS S3 bucket."""
    path = Path(file_path).resolve()
    target_key = s3_key or path.name

    try:
        import boto3  # type: ignore

        s3_client = boto3.client("s3")
        logger.info(f"Uploading '{path}' to s3://{bucket_name}/{target_key}...")
        s3_client.upload_file(str(path), bucket_name, target_key)
        logger.info("S3 upload successful.")
        return True
    except ImportError:
        logger.warning("boto3 package not installed. S3 upload skipped.")
        return False
    except Exception as err:
        logger.error(f"Failed to upload to S3: {err}")
        raise SyncError(f"S3 upload failed: {err}") from err


def download_from_s3(
    bucket_name: str,
    s3_key: str,
    destination_file: str | Path,
) -> bool:
    """Optional helper to download an encrypted archive from an AWS S3 bucket."""
    dest = Path(destination_file).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        import boto3  # type: ignore

        s3_client = boto3.client("s3")
        logger.info(f"Downloading s3://{bucket_name}/{s3_key} to '{dest}'...")
        s3_client.download_file(bucket_name, s3_key, str(dest))
        logger.info("S3 download successful.")
        return True
    except ImportError:
        logger.warning("boto3 package not installed. S3 download skipped.")
        return False
    except Exception as err:
        logger.error(f"Failed to download from S3: {err}")
        raise SyncError(f"S3 download failed: {err}") from err


def main() -> None:
    """CLI interface for sync and archive management."""
    parser = argparse.ArgumentParser(
        description="Encrypted memory archive and sync utility for agent-brain."
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Command: generate-key
    subparsers.add_parser(
        "generate-key", help="Generate a new Fernet AES encryption key"
    )

    # Command: export
    export_parser = subparsers.add_parser(
        "export", help="Compress and encrypt .agent-memory into memory-seed.enc"
    )
    export_parser.add_argument(
        "--source", default=DEFAULT_SOURCE_DIR, help="Source directory to archive"
    )
    export_parser.add_argument(
        "--output", default=DEFAULT_ARCHIVE_FILE, help="Output encrypted file path"
    )
    export_parser.add_argument(
        "--key", default=None, help="Fernet key (or use AGENT_MEMORY_SECRET_KEY env var)"
    )

    # Command: import
    import_parser = subparsers.add_parser(
        "import", help="Decrypt and extract memory-seed.enc into .agent-memory"
    )
    import_parser.add_argument(
        "--archive", default=DEFAULT_ARCHIVE_FILE, help="Encrypted archive path"
    )
    import_parser.add_argument(
        "--target", default=DEFAULT_SOURCE_DIR, help="Target extraction directory"
    )
    import_parser.add_argument(
        "--key", default=None, help="Fernet key (or use AGENT_MEMORY_SECRET_KEY env var)"
    )

    # Command: hydrate
    hydrate_parser = subparsers.add_parser(
        "hydrate", help="Hydrate local database on startup if missing"
    )
    hydrate_parser.add_argument(
        "--archive", default=DEFAULT_ARCHIVE_FILE, help="Encrypted archive path"
    )
    hydrate_parser.add_argument(
        "--target", default=DEFAULT_SOURCE_DIR, help="Target extraction directory"
    )
    hydrate_parser.add_argument(
        "--force", action="store_true", help="Force overwrite existing database"
    )

    args = parser.parse_args()

    if args.command == "generate-key":
        key = generate_encryption_key()
        print(f"Generated Key:\n{key}")
        print("\nSet this as your environment variable:")
        print(f"export {ENV_SECRET_KEY}='{key}'   # Linux / macOS / WSL")
        print(f"$env:{ENV_SECRET_KEY}='{key}'      # Windows PowerShell")
    elif args.command == "export":
        out = export_memory_archive(
            source_dir=args.source, output_file=args.output, secret_key=args.key
        )
        print(f"Archive encrypted and saved to: {out}")
    elif args.command == "import":
        target = import_memory_archive(
            archive_file=args.archive, target_dir=args.target, secret_key=args.key
        )
        print(f"Archive decrypted and restored to: {target}")
    elif args.command == "hydrate":
        hydrated = hydrate_database_on_startup(
            archive_file=args.archive, target_dir=args.target, force=args.force
        )
        print(f"Hydration performed: {hydrated}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
