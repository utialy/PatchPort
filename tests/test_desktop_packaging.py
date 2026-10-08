import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile


SOURCE = Path(__file__).resolve().parents[1] / 'packaging/windows/assemble_probe.py'
spec = importlib.util.spec_from_file_location('assemble_probe', SOURCE)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class DesktopPackagingTests(unittest.TestCase):
    def test_untrusted_runtime_rejected_before_destination_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / 'runtime.zip'
            runtime.write_bytes(b'not the official runtime')
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                builder.assemble(runtime, root / 'missing.whl', root / 'output')
            self.assertFalse((root / 'output').exists())

    def test_archive_traversal_and_case_collisions_rejected(self):
        for names in (['../escape'], ['/absolute'], ['a\\b'], ['C:/escape'], ['A', 'a']):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'test.zip'
                with zipfile.ZipFile(path, 'w') as archive:
                    for name in names: archive.writestr(name, b'example')
                if names == ['a\\b']:
                    # Windows normalizes separators when constructing ZipInfo.
                    path.write_bytes(path.read_bytes().replace(b'a/b', b'a\\b'))
                with zipfile.ZipFile(path) as archive:
                    with self.assertRaises(ValueError): list(builder.entries(archive))

    def test_archive_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.zip'
            with zipfile.ZipFile(path, 'w') as archive:
                link = zipfile.ZipInfo('link')
                link.create_system = 3
                link.external_attr = 0o120777 << 16
                archive.writestr(link, 'target')
            with zipfile.ZipFile(path) as archive:
                with self.assertRaises(ValueError): list(builder.entries(archive))
