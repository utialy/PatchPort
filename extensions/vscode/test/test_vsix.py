import importlib.util
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('verify', Path(__file__).resolve().parents[1] / 'scripts/verify-vsix.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)

class NativeArchitectureTests(unittest.TestCase):
    def test_elf_target_rejects_another_cpu_even_when_filename_matches(self):
        elf = bytearray(64)
        elf[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<H', elf, 18, 62)
        verify.binary_arch(elf, 'linux-x64')
        with self.assertRaisesRegex(ValueError, 'architecture'):
            verify.binary_arch(elf, 'linux-arm64')

    def test_macho_target_rejects_another_cpu(self):
        macho = b'\xcf\xfa\xed\xfe' + struct.pack('<I', 0x0100000c) + bytes(24)
        verify.binary_arch(macho, 'darwin-arm64')
        with self.assertRaisesRegex(ValueError, 'architecture'):
            verify.binary_arch(macho, 'darwin-x64')

    def test_native_elf_cannot_be_distributed_as_macos(self):
        with self.assertRaises(ValueError):
            verify.binary_arch(b'\x7fELF' + bytes(60), 'darwin-x64')


class LocalHelperPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='patchport-helper-verifier-')
        self.addCleanup(self.temporary.cleanup)
        self.file = Path(self.temporary.name) / 'fixture.zip'

    def fixture(self, local=True, configuration_array=False):
        package = {'version': '0.5.0', 'icon': 'media/symbol.png'}
        if local:
            setting = {'properties': {'patchport.localProfiles': {'type': 'array'}}}
            package['contributes'] = {'configuration': [setting] if configuration_array else setting}
        native_path = 'node_modules/node-pty/prebuilds/linux-x64/pty.node'
        elf = bytearray(64)
        elf[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<H', elf, 18, 62)
        core_path, core = 'core/agent_bridge/__init__.py', b'# Fixture runtime.\n'
        assets = {
            'extension.vsixmanifest': b'<PackageManifest xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011"><Metadata><Identity Version="0.5.0" TargetPlatform="linux-x64" /></Metadata></PackageManifest>',
            'extension/package.json': json.dumps(package).encode(),
            'extension/media/symbol.png': (Path(__file__).resolve().parents[1] / 'media/symbol.png').read_bytes(),
            'extension/' + native_path: bytes(elf),
            'extension/native-manifest.json': json.dumps({'schema': 1, 'target': 'linux-x64', 'node_pty': '1.1.0',
                'files': [{'path': native_path, 'sha256': hashlib.sha256(elf).hexdigest()}]}).encode(),
            'extension/' + core_path: core,
            'extension/runtime-manifest.json': json.dumps({'schema': 1, 'extension_version': '0.5.0',
                'files': [{'path': core_path, 'sha256': hashlib.sha256(core).hexdigest()}]}).encode(),
        }
        if local:
            files = []
            for name in sorted(verify.LOCAL_HELPERS):
                data = ('// Fixture: ' + name + '\n').encode()
                assets['extension/' + name] = data
                files.append({'path': name, 'sha256': hashlib.sha256(data).hexdigest()})
            assets['extension/local-helper-manifest.json'] = json.dumps({'schema': 1, 'files': files}).encode()
        return assets

    def check(self, assets):
        with zipfile.ZipFile(self.file, 'w') as archive:
            for name, data in assets.items():
                archive.writestr(name, data)
        return verify.verify(self.file, 'linux-x64')

    def test_historical_package_without_local_assets_or_setting_needs_no_manifest(self):
        result = self.check(self.fixture(local=False))
        self.assertTrue(result['passed'])
        self.assertEqual(result['local_helper_files'], 0)

    def test_local_package_verifies_exact_two_helpers_for_object_and_array_configuration(self):
        for array in (False, True):
            with self.subTest(configuration_array=array):
                self.assertEqual(self.check(self.fixture(configuration_array=array))['local_helper_files'], 2)

    def test_local_setting_or_any_local_asset_requires_manifest(self):
        for trigger in ('setting', 'asset'):
            with self.subTest(trigger=trigger):
                assets = self.fixture(local=False)
                if trigger == 'asset':
                    assets['extension/media/local-api.cjs'] = b'// Fixture.\n'
                else:
                    package = json.loads(assets['extension/package.json'])
                    package['contributes'] = {'configuration': [{'properties': {'patchport.localProfiles': {}}}]}
                    assets['extension/package.json'] = json.dumps(package).encode()
                with self.assertRaisesRegex(ValueError, 'Missing local helper manifest'):
                    self.check(assets)

    def test_missing_extra_duplicate_and_path_traversal_entries_are_rejected(self):
        for mutation in ('missing', 'extra', 'duplicate', 'path'):
            with self.subTest(mutation=mutation):
                assets = self.fixture()
                manifest = json.loads(assets['extension/local-helper-manifest.json'])
                if mutation == 'missing':
                    manifest['files'].pop()
                elif mutation == 'extra':
                    manifest['files'].append({'path': 'media/local-extra.cjs', 'sha256': '0' * 64})
                elif mutation == 'duplicate':
                    manifest['files'][1] = manifest['files'][0].copy()
                else:
                    manifest['files'][0]['path'] = '../media/local-api.cjs'
                assets['extension/local-helper-manifest.json'] = json.dumps(manifest).encode()
                with self.assertRaises(ValueError):
                    self.check(assets)

    def test_missing_modified_and_unlisted_helper_assets_are_rejected(self):
        for mutation in ('missing', 'modified', 'unlisted', 'case_alias', 'manifest_case_alias'):
            with self.subTest(mutation=mutation):
                assets = self.fixture()
                if mutation == 'missing':
                    del assets['extension/media/local-terminal.cjs']
                elif mutation == 'modified':
                    assets['extension/media/local-api.cjs'] += b'// Changed after manifest.\n'
                elif mutation == 'case_alias':
                    assets['extension/media/LOCAL-API.cjs'] = b'// Case alias.\n'
                elif mutation == 'manifest_case_alias':
                    assets['extension/LOCAL-HELPER-MANIFEST.JSON'] = assets['extension/local-helper-manifest.json']
                else:
                    assets['extension/media/local-extra.cjs'] = b'// Unlisted helper.\n'
                with self.assertRaises(ValueError):
                    self.check(assets)

    def test_manifest_schema_and_hash_types_are_strict(self):
        for mutation in ('boolean_schema', 'future_schema', 'files_object', 'hash_number', 'hash_short', 'hash_uppercase'):
            with self.subTest(mutation=mutation):
                assets = self.fixture()
                manifest = json.loads(assets['extension/local-helper-manifest.json'])
                if mutation == 'boolean_schema':
                    manifest['schema'] = True
                elif mutation == 'future_schema':
                    manifest['schema'] = 2
                elif mutation == 'files_object':
                    manifest['files'] = {}
                else:
                    manifest['files'][0]['sha256'] = {'hash_number': 1, 'hash_short': 'a', 'hash_uppercase': 'A' * 64}[mutation]
                assets['extension/local-helper-manifest.json'] = json.dumps(manifest).encode()
                with self.assertRaises(ValueError):
                    self.check(assets)

    def test_duplicate_json_keys_are_rejected_instead_of_overwriting_the_manifest(self):
        assets = self.fixture()
        manifest = assets['extension/local-helper-manifest.json'].decode()
        assets['extension/local-helper-manifest.json'] = manifest.replace('"schema": 1', '"schema": 2, "schema": 1').encode()
        with self.assertRaisesRegex(ValueError, 'Duplicate local helper manifest key'):
            self.check(assets)

if __name__ == '__main__':
    unittest.main()
