#!/usr/bin/env python3
"""Generate a TOTP code from a Base32 secret. RFC 6238 / SHA1 / 30s / 6 digits."""
import argparse
import base64
import hmac
import hashlib
import struct
import subprocess
import sys
import time


def totp(secret: str, digits: int = 6, period: int = 30) -> str:
    cleaned = secret.upper().replace(' ', '').replace('=', '')
    padding = '=' * (-len(cleaned) % 8)
    key = base64.b32decode(cleaned + padding)
    counter = struct.pack('>Q', int(time.time() // period))
    mac = hmac.new(key, counter, hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = struct.unpack('>I', mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def copy_to_clipboard(text: str) -> None:
    for cmd in (['wl-copy'], ['xclip', '-selection', 'clipboard']):
        try:
            subprocess.run(cmd, input=text.encode(), check=True)
            return
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    print('warning: neither wl-copy nor xclip found; not copied', file=sys.stderr)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('secret', nargs='?', help='Base32 secret (use - to read stdin)')
    p.add_argument('-f', '--file', help='read secret from FILE')
    p.add_argument('-c', '--copy', action='store_true', help='copy code to clipboard')
    args = p.parse_args()

    if args.file:
        with open(args.file) as f:
            secret = f.read().strip()
    elif args.secret == '-':
        secret = sys.stdin.read().strip()
    elif args.secret:
        secret = args.secret.strip()
    else:
        p.print_usage(sys.stderr)
        return 1

    code = totp(secret)
    print(code)
    if args.copy:
        copy_to_clipboard(code)
    return 0


if __name__ == '__main__':
    sys.exit(main())
