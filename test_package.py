import unittest
from pathlib import Path
from audit_package import check_bytes


class PackagePrivacyTests(unittest.TestCase):
    def test_known_private_content_is_rejected(self):
        for data in [b'/Users/example/.codex/auth.json', b'-----BEGIN PRIVATE KEY-----',
                     b'sk-ant-' + b'x' * 40]:
            with self.assertRaises(ValueError):
                check_bytes(data, 'fixture')

    def test_clean_source_is_accepted(self):
        check_bytes(b"Path.home() / 'Library/Application Support/Subscription Pin'", 'fixture')

    def test_compact_panel_keeps_resizable_layout_contract(self):
        source = (Path(__file__).parent / 'main.swift').read_text()
        self.assertIn('.nonactivatingPanel, .resizable', source)
        self.assertIn('savedPanelSize(compact:', source)
        self.assertIn('width - 8 - controlsWidth', source)
        self.assertNotIn('width: 180, height: height), display: true', source)

    def test_expanded_brand_copy_and_alignment(self):
        source = (Path(__file__).parent / 'main.swift').read_text()
        self.assertIn('row([spacer(), brandLogo(size: 16)', source)
        self.assertIn('label("言回有限公司", size: 10', source)


if __name__ == '__main__':
    unittest.main()
