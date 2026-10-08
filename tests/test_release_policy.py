from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import struct
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import check_public_release as policy


class ReleasePolicyTests(unittest.TestCase):
    def png(self):
        def chunk(kind, body):
            return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)
        return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\x00\x00\x00\x00')) + chunk(b'IEND', b'')

    def test_only_reviewed_screenshot_paths_accept_valid_png(self):
        data = self.png()
        self.assertEqual(policy.check_content('docs/images/board.png', data), [])
        self.assertTrue(policy.check_content('examples/board.png', data))
        self.assertTrue(policy.check_content('docs/images/board.png', b'not a PNG'))
        self.assertTrue(policy.check_content('docs/images/board.png', data[:-1]))
        self.assertTrue(policy.check_content('docs/images/board.png', data + b'unreviewed'))

    def test_extension_layout_keeps_generated_and_private_files_excluded(self):
        self.assertEqual(policy.check_content('extensions/vscode/src/example.ts', b'export const value = 1;\n'), [])
        self.assertEqual(policy.check_content('extensions/vscode/.vscodeignore', b'dist/test/**\n'), [])
        self.assertTrue(policy.check_content('extensions/vscode/core/agent_bridge/x.py', b'value = 1\n'))
        self.assertTrue(policy.check_content('extensions/vscode/runtime-manifest.json', b'{}'))
        self.assertTrue(policy.check_content('extensions/vscode/dist/bundle.js', b'value = 1;'))

    def test_valid_commit_message(self):
        self.assertEqual(policy.check_message('fix(archive): reject corrupt blobs\n\nValidate the digest before reading evidence.\n'), [])

    def test_invalid_commit_messages(self):
        for message in ('', 'update files', 'fix: x\nbody', 'fix: '+('a'*80), 'docs: '+chr(0xD55C)):
            with self.subTest(message=message):
                self.assertTrue(policy.check_message(message))

    def test_private_paths_rejected(self):
        for name in ('.env', '.bridge/events.jsonl', 'docs/HANDOFF.md', 'src/__pycache__/a.pyc', '../outside.md'):
            with self.subTest(name=name):
                self.assertTrue(policy.check_content(name, b'English text\n'))

    def test_unicode_payload_requires_source_escapes(self):
        self.assertTrue(policy.check_content('docs/example.md', chr(0xD55C).encode()))
        self.assertEqual(policy.check_content('tests/example.py', b"value = '\\uD55C'\n"), [])

    def test_credential_pattern_rejected(self):
        self.assertTrue(policy.check_content('examples/key.md', ('sk-'+('a'*32)).encode()))

    def test_link_check_uses_repository_files(self):
        files = {name:b'English\n' for name in policy.REQUIRED}
        files['README.md'] = b'[Rules](docs/COMMIT_RULES.md)\n'
        self.assertEqual(policy.check_files(files), [])
        files['README.md'] = b'[Missing](../private.md)\n'
        self.assertTrue(any('unresolved' in error for error in policy.check_files(files)))

    def test_staged_bytes_take_precedence_over_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            for name in policy.REQUIRED:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'English\n')
            (root/'README.md').write_text(chr(0xD55C), encoding='utf-8')
            subprocess.run(['git', 'add', '--all'], cwd=root, check=True, capture_output=True)
            (root/'README.md').write_text('English\n')
            self.assertTrue(any('ASCII' in error for error in policy.check_files(policy.staged_files(root))))
            subprocess.run(['git', 'add', 'README.md'], cwd=root, check=True, capture_output=True)
            (root/'README.md').write_text(chr(0xD55C), encoding='utf-8')
            self.assertEqual(policy.check_files(policy.staged_files(root)), [])


if __name__ == '__main__':
    unittest.main()
