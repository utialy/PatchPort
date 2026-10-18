import importlib.util
from pathlib import Path
import struct
import unittest

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

if __name__ == '__main__':
    unittest.main()
