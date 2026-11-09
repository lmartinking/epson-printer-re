#!/usr/bin/env python3
"""Verify SHA-1 offset calculations match diff locations."""
print('RCX header: 291 =', hex(291))
print('IPL header: 0x100 =', 0x100)
print('Record 0 SHA-1 offset: 0x20 =', 0x20)
print('Total:', 291 + 0x100 + 0x20, '=', hex(291 + 0x100 + 0x20))
z1 = 7483392
offset_b1_seg0_sha = 291 + z1 + 0x20
print()
print('Block 1: RCX header (291) + Block 0 Z_1 (' + str(z1) + ') + record 0 SHA-1 (0x20) =', offset_b1_seg0_sha, '=', hex(offset_b1_seg0_sha))
