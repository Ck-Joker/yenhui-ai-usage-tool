import unittest
from audit_package import check_bytes


class PackagePrivacyTests(unittest.TestCase):
    def test_known_private_content_is_rejected(self):
        for data in [b'/Users/example/.codex/auth.json', b'-----BEGIN PRIVATE KEY-----',
                     b'sk-ant-' + b'x' * 40]:
            with self.assertRaises(ValueError):
                check_bytes(data, 'fixture')

    def test_clean_source_is_accepted(self):
        check_bytes(b"Path.home() / 'Library/Application Support/Subscription Pin'", 'fixture')


if __name__ == '__main__':
    unittest.main()
