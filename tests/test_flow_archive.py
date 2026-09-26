import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import flow_archive


class FlowArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / '.role-flows/sample/archive'
        (self.archive / 'blobs').mkdir(parents=True)
        records = b'{"original_phase":"REVIEW_DONE"}'
        (self.archive / 'records.json').write_bytes(records)
        self.manifest = dict(schema=1, flow='sample', entries=[],
                             records_sha256=hashlib.sha256(records).hexdigest())
        self.add('log.txt', b'Ran 9 tests\r\nOK\n')

    def add(self, name, data):
        digest = hashlib.sha256(data).hexdigest()
        (self.archive / 'blobs' / digest).write_bytes(data)
        self.manifest['entries'].append(dict(source='reviewer_workspace', path=name,
            kind='file', sha256=digest, size=len(data), mtime_ns=1))
        self.save()

    def save(self):
        self.manifest['archive_digest'] = flow_archive.manifest_digest(self.manifest)
        (self.archive / 'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')

    def read(self, path='log.txt'):
        return flow_archive.read_evidence(self.root, 'sample', 'reviewer_workspace', path)

    def test_text_and_read_only(self):
        def snapshot():
            return {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in self.root.rglob('*') if p.is_file()}
        before = snapshot()
        self.assertEqual(self.read()['content'], 'Ran 9 tests\r\nOK\n')
        self.assertEqual(before, snapshot())

    def test_binary_and_empty(self):
        self.add('raw.bin', b'\xff\x00')
        self.add('empty.txt', b'')
        self.assertEqual((self.read('raw.bin')['encoding'], self.read('raw.bin')['content']), ('base64', '/wA='))
        self.assertEqual(self.read('empty.txt')['content'], '')

    def test_blob_and_records_corruption(self):
        blob = self.archive / 'blobs' / self.manifest['entries'][0]['sha256']
        original = blob.read_bytes()
        blob.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'Evidence digest'): self.read()
        blob.write_bytes(original)
        (self.archive / 'records.json').write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'Records digest'): self.read()

    def test_manifest_digest_and_identity(self):
        self.manifest['flow'] = 'different'
        self.save()
        with self.assertRaises(ValueError): self.read()
        self.manifest['flow'] = 'sample'
        (self.archive / 'manifest.json').write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, 'manifest digest'): self.read()

    def test_unsafe_paths_and_duplicate_entries(self):
        for path in ('../secret', '/absolute', 'a/../b', 'a\\b', 'C:/x', './x', 'a//b', ''):
            with self.subTest(path=path), self.assertRaises(ValueError): self.read(path)
        self.manifest['entries'].append(dict(self.manifest['entries'][0]))
        self.save()
        with self.assertRaisesRegex(ValueError, 'Duplicate evidence'): self.read()

    def test_invalid_json_and_schema(self):
        for raw in ('{"schema":1,"schema":1}', '{"schema":NaN}'):
            (self.archive / 'manifest.json').write_text(raw)
            with self.assertRaises(ValueError): self.read()
        self.manifest['schema'] = True
        self.save()
        with self.assertRaises(ValueError): self.read()

    def test_missing_never_creates_paths(self):
        before = set(self.root.rglob('*'))
        with self.assertRaises(OSError): flow_archive.load_archive(self.root, 'missing')
        with self.assertRaisesRegex(ValueError, 'Evidence not found'): self.read('missing')
        self.assertEqual(before, set(self.root.rglob('*')))


    def test_directory_blob_rejected(self):
        blob = self.archive / 'blobs' / self.manifest['entries'][0]['sha256']
        blob.unlink()
        blob.mkdir()
        with self.assertRaisesRegex(ValueError, 'regular file'): self.read()

    def test_unsafe_stored_path_rejected(self):
        self.manifest['entries'][0]['path'] = '../outside'
        self.save()
        with self.assertRaisesRegex(ValueError, 'logical evidence path'): self.read()


if __name__ == '__main__':
    unittest.main()
