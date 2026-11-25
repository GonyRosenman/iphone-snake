"""Inspect the actual device IPA's loader contract, not Xcode source settings."""
import hashlib
import json
from pathlib import Path
import plistlib
import struct
import sys
import zipfile


def verify(path):
    data = Path(path).read_bytes()
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None, 'Corrupt IPA archive'
        root = 'Payload/HomeVoice.app/'
        info = plistlib.loads(archive.read(root+'Info.plist'))
        assert info['DTPlatformName'] == 'iphoneos', 'A device app is required'
        assert info['NSMicrophoneUsageDescription'], 'Microphone permission description missing'
        binary = archive.read(root+info['CFBundleExecutable'])
        magic, cpu = struct.unpack_from('<II', binary)
        assert magic == 0xfeedfacf and cpu == 0x0100000c, 'Expected arm64 Mach-O'
        offset = 32; count = struct.unpack_from('<I', binary, 16)[0]
        rpaths, libraries = [], []
        for _ in range(count):
            command, size = struct.unpack_from('<II', binary, offset)
            assert size >= 8 and offset+size <= len(binary), 'Invalid Mach-O load command'
            if command in (0x8000001c, 0xc, 0x80000018, 0x8000001f):
                text_offset = struct.unpack_from('<I', binary, offset+8)[0]
                value = binary[offset+text_offset:offset+size].split(b'\0')[0].decode()
                (rpaths if command == 0x8000001c else libraries).append(value)
            offset += size
        assert '@rpath/WebRTC.framework/WebRTC' in libraries, 'WebRTC dependency missing'
        assert '@executable_path/Frameworks' in rpaths, 'Embedded WebRTC cannot load: Frameworks runpath missing'
        assert root+'Frameworks/WebRTC.framework/WebRTC' in archive.namelist(), 'WebRTC binary not embedded'
        assert root+'ThirdPartyNotices.txt' in archive.namelist(), 'Dependency notices missing'
        return {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'architecture': 'arm64',
                'platform': info['DTPlatformName'], 'minimum_ios': info['MinimumOSVersion'],
                'bundle_id': info['CFBundleIdentifier'], 'framework_embedded': True,
                'framework_loader_path_valid': True, 'notices_included': True,
                'physical_device': 'not tested'}


if __name__ == '__main__':
    print(json.dumps(verify(sys.argv[1]), indent=2))
