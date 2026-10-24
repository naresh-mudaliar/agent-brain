"""Unit tests for sync.s3_sync."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from sync.s3_sync import (
    ENV_SECRET_KEY,
    DecryptionError,
    EncryptionError,
    export_memory_archive,
    generate_encryption_key,
    hydrate_database_on_startup,
    import_memory_archive,
)


class TestS3Sync(unittest.TestCase):
    """Test suite for sync.s3_sync archive encryption and hydration."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.source_dir = Path(self.test_dir) / "source_memory"
        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.archive_file = Path(self.test_dir) / "memory-seed.enc"
        self.restore_dir = Path(self.test_dir) / "restored_memory"

        # Create dummy database and metadata files
        (self.source_dir / "brain.db").write_text("DUMMY_AGENT_DATABASE_CONTENT")
        sub_dir = self.source_dir / "indices"
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "meta.json").write_text('{"version": 1, "graph": "CompanyBrain"}')

        self.key = generate_encryption_key()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        if ENV_SECRET_KEY in os.environ:
            del os.environ[ENV_SECRET_KEY]

    def test_generate_encryption_key(self):
        """Test that generated key is valid for Fernet."""
        key = generate_encryption_key()
        self.assertIsInstance(key, str)
        # Should initialize Fernet without error
        fernet = Fernet(key.encode("utf-8"))
        token = fernet.encrypt(b"test")
        self.assertEqual(fernet.decrypt(token), b"test")

    def test_missing_secret_key_raises_value_error(self):
        """Test that missing secret key raises ValueError."""
        if ENV_SECRET_KEY in os.environ:
            del os.environ[ENV_SECRET_KEY]

        with self.assertRaises(ValueError):
            export_memory_archive(
                source_dir=self.source_dir,
                output_file=self.archive_file,
                secret_key=None,
            )

    def test_roundtrip_export_and_import(self):
        """Test compressing/encrypting and then decrypting/extracting files."""
        # 1. Export
        saved_path = export_memory_archive(
            source_dir=self.source_dir,
            output_file=self.archive_file,
            secret_key=self.key,
        )
        self.assertTrue(saved_path.exists())
        self.assertGreater(saved_path.stat().st_size, 0)

        # Ensure raw file content is encrypted (does NOT contain plaintext)
        raw_bytes = saved_path.read_bytes()
        self.assertNotIn(b"DUMMY_AGENT_DATABASE_CONTENT", raw_bytes)

        # 2. Import
        restored_path = import_memory_archive(
            archive_file=self.archive_file,
            target_dir=self.restore_dir,
            secret_key=self.key,
        )
        self.assertTrue(restored_path.exists())

        # Verify restored files and contents
        restored_db = self.restore_dir / "brain.db"
        self.assertTrue(restored_db.exists())
        self.assertEqual(restored_db.read_text(), "DUMMY_AGENT_DATABASE_CONTENT")

        restored_meta = self.restore_dir / "indices" / "meta.json"
        self.assertTrue(restored_meta.exists())
        self.assertEqual(
            restored_meta.read_text(), '{"version": 1, "graph": "CompanyBrain"}'
        )

    def test_export_import_with_env_var(self):
        """Test encryption/decryption using AGENT_MEMORY_SECRET_KEY environment variable."""
        os.environ[ENV_SECRET_KEY] = self.key

        export_memory_archive(
            source_dir=self.source_dir, output_file=self.archive_file
        )
        self.assertTrue(self.archive_file.exists())

        import_memory_archive(
            archive_file=self.archive_file, target_dir=self.restore_dir
        )
        self.assertTrue((self.restore_dir / "brain.db").exists())

    def test_import_with_wrong_key_raises_decryption_error(self):
        """Test that importing with wrong key raises DecryptionError."""
        export_memory_archive(
            source_dir=self.source_dir,
            output_file=self.archive_file,
            secret_key=self.key,
        )

        wrong_key = generate_encryption_key()
        with self.assertRaises(DecryptionError):
            import_memory_archive(
                archive_file=self.archive_file,
                target_dir=self.restore_dir,
                secret_key=wrong_key,
            )

    def test_import_tampered_archive_raises_decryption_error(self):
        """Test that tampered archive bytes raise DecryptionError."""
        export_memory_archive(
            source_dir=self.source_dir,
            output_file=self.archive_file,
            secret_key=self.key,
        )

        # Tamper with file
        raw = bytearray(self.archive_file.read_bytes())
        raw[20] ^= 0xFF
        self.archive_file.write_bytes(bytes(raw))

        with self.assertRaises(DecryptionError):
            import_memory_archive(
                archive_file=self.archive_file,
                target_dir=self.restore_dir,
                secret_key=self.key,
            )

    def test_hydrate_database_on_startup(self):
        """Test startup hydration behavior."""
        # Scenario 1: Archive exists, database missing -> Hydrates
        export_memory_archive(
            source_dir=self.source_dir,
            output_file=self.archive_file,
            secret_key=self.key,
        )

        target = Path(self.test_dir) / "startup_memory"
        hydrated = hydrate_database_on_startup(
            archive_file=self.archive_file,
            target_dir=target,
            secret_key=self.key,
        )
        self.assertTrue(hydrated)
        self.assertTrue((target / "brain.db").exists())

        # Scenario 2: Database already exists and force=False -> Skips hydration
        hydrated_again = hydrate_database_on_startup(
            archive_file=self.archive_file,
            target_dir=target,
            secret_key=self.key,
            force=False,
        )
        self.assertFalse(hydrated_again)

        # Scenario 3: Database already exists but force=True -> Hydrates again
        hydrated_forced = hydrate_database_on_startup(
            archive_file=self.archive_file,
            target_dir=target,
            secret_key=self.key,
            force=True,
        )
        self.assertTrue(hydrated_forced)


if __name__ == "__main__":
    unittest.main()
