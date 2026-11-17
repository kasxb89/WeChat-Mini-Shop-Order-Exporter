import hashlib
from pathlib import Path
import tempfile
import unittest

from deployment.configure_activation import write_config


class BuildConfigurationTests(unittest.TestCase):
    def test_generated_private_config_contains_only_the_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'_private_config.py'
            write_config('  build-test-code  ',target)
            text=target.read_text(encoding='utf-8')
            self.assertNotIn('build-test-code',text)
            self.assertIn(hashlib.sha256(b'build-test-code').hexdigest(),text)

    def test_blank_input_does_not_create_a_build_config(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'_private_config.py'
            with self.assertRaises(ValueError):write_config('  ',target)
            self.assertFalse(target.exists())


if __name__=='__main__':unittest.main()
