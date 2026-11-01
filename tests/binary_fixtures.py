"""Hand-built compiled files for the native binary inspector.

A test that needs a real `gcc` or `objcopy` is a test that skips on a machine without one, and this repository does
not let a test skip out of a guarantee. These are written byte by byte with `struct`, so the sections, symbols,
imports, strings and calls the inspector is asserted against are all visible in this file.

`elf64` is a small x86-64 executable-shaped file: a `.text` with three named functions and two direct calls, a
`.rodata` with two strings, a `.comment` naming GCC, a `.dynsym` importing `puts` from `libc.so.6`, and section
headers pointing at all of it. It is not runnable — nothing here is meant to be — and every offset is chosen so the
fixture is easy to audit rather than minimal.
"""

from __future__ import annotations

import struct


def elf64() -> bytes:
    """A 64-bit little-endian ELF with .text, .rodata, .comment, .dynstr, .dynsym, .dynamic, .symtab and .strtab."""
    buffer = bytearray(0x680)

    def put(offset: int, data: bytes) -> None:
        buffer[offset:offset + len(data)] = data

    ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
    struct.pack_into(
        "<16sHHIQQQIHHHHHH", buffer, 0,
        ident, 2, 62, 1, 0x401000, 0, 0x400, 0, 64, 0, 0, 64, 10, 9,
    )

    text = bytearray(0x40)
    text[0:12] = bytes([0xE8, 0x0B, 0, 0, 0, 0xB8, 0x3C, 0, 0, 0, 0x0F, 0x05])
    text[0x10:0x16] = bytes([0xE8, 0x05, 0, 0, 0, 0xC3])
    text[0x1A] = 0xC3
    put(0x100, bytes(text))
    put(0x180, b"hello world\x00/etc/passwd\x00")
    put(0x1A0, b"GCC: (test) 13.2.0\x00")
    put(0x1C0, b"puts\x00libc.so.6\x00")

    dynsym = bytearray(48)
    struct.pack_into("<IBBHQQ", dynsym, 24, 0, 0x12, 0, 0, 0, 0)
    put(0x1E0, bytes(dynsym))

    dynamic = bytearray(32)
    struct.pack_into("<qQ", dynamic, 0, 1, 5)  # DT_NEEDED, "libc.so.6"
    struct.pack_into("<qQ", dynamic, 16, 0, 0)  # DT_NULL
    put(0x210, bytes(dynamic))

    symbols = bytearray(96)
    struct.pack_into("<IBBHQQ", symbols, 24, 1, 0x12, 0, 1, 0x401000, 12)   # _start
    struct.pack_into("<IBBHQQ", symbols, 48, 8, 0x12, 0, 1, 0x401010, 6)    # main
    struct.pack_into("<IBBHQQ", symbols, 72, 13, 0x12, 0, 1, 0x40101A, 1)   # helper
    put(0x240, bytes(symbols))
    put(0x2D0, b"\x00_start\x00main\x00helper\x00")

    names = b"\x00.text\x00.rodata\x00.comment\x00.dynstr\x00.dynsym\x00.dynamic\x00.symtab\x00.strtab\x00.shstrtab\x00"
    put(0x300, names)

    # (name offset, type, flags, address, file offset, size, link, info, alignment, entry size)
    section_headers = (
        (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        (1, 1, 0x6, 0x401000, 0x100, 0x40, 0, 0, 16, 0),
        (7, 1, 0x2, 0x402000, 0x180, 0x18, 0, 0, 8, 0),
        (15, 1, 0, 0x402040, 0x1A0, 0x13, 0, 0, 1, 0),
        (24, 3, 0x2, 0x402080, 0x1C0, 0x10, 0, 0, 1, 0),
        (32, 11, 0x2, 0x4020C0, 0x1E0, 0x30, 4, 1, 8, 24),
        (40, 6, 0x2, 0x402100, 0x210, 0x20, 4, 0, 8, 16),
        (49, 2, 0, 0x402140, 0x240, 0x60, 8, 1, 8, 24),
        (57, 3, 0, 0x4021D0, 0x2D0, 0x14, 0, 0, 1, 0),
        (65, 3, 0, 0, 0x300, len(names), 0, 0, 1, 0),
    )
    for index, header in enumerate(section_headers):
        struct.pack_into("<IIQQQQIIQQ", buffer, 0x400 + index * 64, *header)
    return bytes(buffer)


def pe64() -> bytes:
    """A PE32+ header with no sections: enough for the summary, and a format the native reader accepts."""
    buffer = bytearray(0x160)
    buffer[0:2] = b"MZ"
    struct.pack_into("<I", buffer, 0x3C, 0x40)
    buffer[0x40:0x44] = b"PE\x00\x00"
    struct.pack_into("<HHIIIHH", buffer, 0x44, 0x8664, 0, 0, 0, 0, 240, 0x0022)
    optional = 0x58
    struct.pack_into("<H", buffer, optional, 0x20B)
    struct.pack_into("<I", buffer, optional + 16, 0)
    struct.pack_into("<Q", buffer, optional + 24, 0x140000000)
    struct.pack_into("<I", buffer, optional + 108, 16)
    return bytes(buffer)


def macho64() -> bytes:
    """A 64-bit little-endian Mach-O executable with no load commands."""
    return struct.pack("<IiiIIIII", 0xFEEDFACF, 0x01000007, 3, 2, 0, 0, 0, 0)
