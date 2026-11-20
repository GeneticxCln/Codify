"""The engine's own reader for a compiled file: ELF, PE and Mach-O, in Python and nothing else.

`binary_analysis.analyze_binary` used to ask Ghidra. This module reads the file directly and answers the same
inventory questions without a JVM, a project directory, a temporary copy, a child process or a five-minute
deadline:

* **What is it?** The container format, the architecture it was built for, its width, its image base and, for
  ELF, the compiler named in `.comment`.
* **What is in it?** The sections that occupy memory, with their permissions; the functions named by a symbol
  table, export table or Mach-O `nlist`; the symbols it imports from a library; and the printable strings in its
  data.
* **What calls what?** Direct calls are decoded from executable sections (`call rel32` on x86, `BL` on AArch64)
  and tied to the function whose address range contains them. An indirect call has no target in the bytes, so it
  is not guessed at.

**What it does not do.** There is no decompiler, no data-flow analysis, no recovery of functions a stripped binary
no longer names, and no interpretation of an instruction beyond a direct call. A stripped file therefore reports
its imports and strings and few or no functions, which is the honest answer: it says what the file contains, not
what a human would infer from disassembly. `binary_analysis` falls back to Ghidra, when one is configured, only
when this reader refuses the format entirely.

**What it is not allowed to do.** The input is a bytes object that the caller has already bounded and hashed;
this module opens nothing, runs nothing, writes nothing and imports nothing outside the standard library. Every
address and every name is treated as the file's own text: it is stripped of control and invisible characters and
clipped before it is returned, and `binary_analysis` frames the result as data. A malformed file is an
`UnsupportedFormat`; a file that is merely unusual comes back with less in it.

The caps are passed in by `binary_analysis`, so the one place that decides how much of a binary may reach a model
is still `EXPORT_CAPS`.
"""

from __future__ import annotations

import bisect
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from engine.models import strip_invisible

#: Bumped when the shape of what this returns changes, so an old cache is not read as the new shape.
INSPECTOR_VERSION = 1

_TEXT_CLIP = 160
_NULL = b"\x00"

#: Section names whose bytes are metadata rather than content. Symbol and relocation tables are full of short
#: printable runs that are not the program's strings, and a `.comment` is already reported as the compiler.
_SKIP_STRINGS = (
    ".symtab", ".strtab", ".shstrtab", ".dynsym", ".dynstr", ".dynamic", ".rela", ".rel.",
    ".gnu", ".note", ".comment", ".interp", "__LINKEDIT", "__debug", "__eh_frame", ".reloc",
)

_ELF_MACHINES = {
    3: ("x86", 32), 62: ("x86", 64), 40: ("arm", 32), 183: ("arm64", 64),
    243: ("riscv", 32), 258: ("riscv", 64), 8: ("mips", 32), 20: ("ppc", 32), 21: ("ppc", 64),
}


class UnsupportedFormat(ValueError):
    """The bytes are not a compiled file this reader understands.

    `binary_analysis` catches this one exception and decides whether to fall back to Ghidra. It is deliberately
    the only failure this module can raise for a file that is well-formed enough to have been read at all.
    """


@dataclass
class _Section:
    """One piece of the file that occupies memory, in the shape `format_analysis` already knows."""

    name: str
    start: int
    size: int
    file_offset: int
    file_size: int
    perms: str
    executable: bool


@dataclass
class _Image:
    """What a container parser found, before the caps are applied and the answer is built."""

    format: str
    language: str
    compiler: str
    bits: int
    image_base: int
    arch: str
    endian: str
    sections: list[_Section] = field(default_factory=list)
    functions: dict[int, str] = field(default_factory=dict)
    sizes: dict[int, int] = field(default_factory=dict)
    imports: list[tuple[str, str]] = field(default_factory=list)


def _need(data: bytes, offset: int, size: int) -> None:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise UnsupportedFormat("the file is shorter than its own headers say")


def _unpack(data: bytes, endian: str, fmt: str, offset: int) -> tuple[Any, ...]:
    _need(data, offset, struct.calcsize(endian + fmt))
    return struct.unpack_from(endian + fmt, data, offset)


def _bytes_at(data: bytes, offset: int, size: int) -> bytes:
    if offset < 0 or size < 0 or offset + size > len(data):
        return b""
    return data[offset:offset + size]


def _cstr(data: bytes, offset: int, limit: int = 300) -> str:
    """A NUL-terminated string from the file, bounded. The terminator is required; a table entry is not prose."""
    if offset < 0 or offset >= len(data):
        return ""
    end = data.find(_NULL, offset, min(len(data), offset + limit + 1))
    if end < 0:
        return ""
    return data[offset:end].decode("utf-8", "replace")


def _clean(value: str, limit: int = _TEXT_CLIP) -> str:
    """One piece of the file's own text, made safe to put in front of a model."""
    text = strip_invisible(value).replace("\xa0", " ")
    text = "".join(char for char in text if ord(char) >= 0x20 and ord(char) != 0x7F)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _hex(value: int) -> str:
    return f"0x{value:x}" if value else "0"


def _name_is_metadata(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.startswith(prefix.lower()) for prefix in _SKIP_STRINGS)


def _language(arch: str, bits: int, endian: str) -> str:
    suffix = "LE" if endian == "<" else "BE"
    if arch == "x86":
        return f"x86:{suffix}:{bits}:default"
    if arch == "arm64":
        return f"AARCH64:{suffix}:64:v8A"
    if arch == "arm":
        return f"ARM:{suffix}:{bits}:v8"
    if arch == "riscv":
        return f"RISCV:{suffix}:{bits}:default"
    if arch == "mips":
        return f"MIPS:{suffix}:{bits}:default"
    if arch == "ppc":
        return f"PowerPC:{suffix}:{bits}:default"
    return f"{arch or 'unknown'}:{suffix}:{bits}:default"


def _perms(read: bool, write: bool, execute: bool) -> str:
    return ("r" if read else "") + ("w" if write else "") + ("x" if execute else "") or "-"


# ── calling relationships ───────────────────────────────────────────────────────────────────


_PRINTABLE_RUN = re.compile(rb"[\x20-\x7e]{4,}")
_ARM64_BL_TOP = re.compile(rb"[\x94-\x97]")
_MAX_FAT_DEPTH = 4
MAX_SECTIONS = 1024
MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_STRINGS_COUNT = 50_000


def _uncovered_ranges(covered: list[tuple[int, int]], start: int, end: int) -> list[tuple[int, int]]:
    """Return sub-intervals of `[start, end)` not yet in `covered`, and merge `[start, end)` into `covered`."""
    if start >= end:
        return []
    uncovered: list[tuple[int, int]] = []
    cur = start
    merged: list[tuple[int, int]] = []
    inserted = False
    new_lo, new_hi = start, end
    for lo, hi in covered:
        if hi < start:
            merged.append((lo, hi))
            continue
        if lo > end:
            if not inserted:
                if cur < end:
                    uncovered.append((cur, end))
                merged.append((new_lo, new_hi))
                inserted = True
            merged.append((lo, hi))
            continue
        if lo > cur:
            uncovered.append((cur, lo))
        cur = max(cur, hi)
        new_lo = min(new_lo, lo)
        new_hi = max(new_hi, hi)
    if not inserted:
        if cur < end:
            uncovered.append((cur, end))
        merged.append((new_lo, new_hi))
    covered[:] = merged
    return uncovered


def _scan_calls(image: _Image, data: bytes, addrs: list[int], sizes: dict[int, int],
                limits: Mapping[str, int]) -> tuple[dict[int, set[int]], dict[int, set[int]]]:
    """Direct call edges, by scanning executable sections. Indirect calls are not guessed at."""
    out: dict[int, set[int]] = {addr: set() for addr in addrs}
    incoming: dict[int, set[int]] = {addr: set() for addr in addrs}
    if image.arch not in ("x86", "arm64"):
        return out, incoming
    covered: list[tuple[int, int]] = []
    scanned_bytes = 0
    for section in image.sections:
        if not section.executable or section.file_size <= 0:
            continue
        if section.file_offset < 0 or section.file_offset >= len(data):
            continue
        sec_end = min(len(data), section.file_offset + section.file_size)
        for sub_start, sub_end in _uncovered_ranges(covered, section.file_offset, sec_end):
            remaining = MAX_SCAN_BYTES - scanned_bytes
            if remaining <= 0:
                return out, incoming
            if sub_end - sub_start > remaining:
                sub_end = sub_start + remaining
            body = data[sub_start:sub_end]
            scanned_bytes += len(body)
            if len(body) < 5:
                continue
            va_base = section.start + (sub_start - section.file_offset)
            if image.arch == "x86":
                unpack_i = struct.Struct(image.endian + "i").unpack_from
                limit = len(body) - 4
                pos = body.find(b"\xe8", 0, limit)
                while pos != -1:
                    target = va_base + pos + 5 + unpack_i(body, pos + 1)[0]
                    _edge(out, incoming, addrs, sizes, va_base + pos, target, limits["edges"])
                    pos = body.find(b"\xe8", pos + 1, limit)
            else:
                unpack_u = struct.Struct(image.endian + "I").unpack_from
                aligned_len = len(body) - (len(body) % 4)
                top = body[3:aligned_len:4] if image.endian == "<" else body[0:aligned_len:4]
                for match in _ARM64_BL_TOP.finditer(top):
                    index = match.start() * 4
                    word = unpack_u(body, index)[0]
                    immediate = word & 0x03FFFFFF
                    if immediate & 0x02000000:
                        immediate -= 0x04000000
                    target = va_base + index + (immediate << 2)
                    _edge(out, incoming, addrs, sizes, va_base + index, target, limits["edges"])
    return out, incoming


def _edge(out: dict[int, set[int]], incoming: dict[int, set[int]], addrs: list[int],
          sizes: dict[int, int], at: int, target: int, cap: int) -> None:
    caller = _owner(addrs, sizes, at)
    callee = _owner(addrs, sizes, target)
    if caller is None or callee is None or caller == callee:
        return
    if len(out[caller]) >= cap and callee not in out[caller]:
        return
    out[caller].add(callee)
    incoming[callee].add(caller)


def _owner(addrs: list[int], sizes: dict[int, int], address: int) -> int | None:
    index = bisect.bisect_right(addrs, address) - 1
    if index < 0:
        return None
    start = addrs[index]
    size = sizes.get(start, 0)
    if size <= 0:
        return start if address == start else None
    if address >= start + size:
        return None
    return start


# ── strings ─────────────────────────────────────────────────────────────────────────────────


def _strings(image: _Image, data: bytes, cap: int, text_clip: int) -> tuple[list[dict[str, str]], int]:
    """Printable runs of four characters or more in the sections that hold content, bounded."""
    rows: list[dict[str, str]] = []
    total = 0
    covered: list[tuple[int, int]] = []
    scanned_bytes = 0
    count_limit = max(cap * 4, MAX_STRINGS_COUNT)
    for section in image.sections:
        if section.file_size <= 0 or _name_is_metadata(section.name):
            continue
        if section.file_offset < 0 or section.file_offset >= len(data):
            continue
        sec_end = min(len(data), section.file_offset + section.file_size)
        for sub_start, sub_end in _uncovered_ranges(covered, section.file_offset, sec_end):
            remaining = MAX_SCAN_BYTES - scanned_bytes
            if remaining <= 0 or total >= count_limit:
                return rows, total
            if sub_end - sub_start > remaining:
                sub_end = sub_start + remaining
            body = data[sub_start:sub_end]
            scanned_bytes += len(body)
            va_base = section.start + (sub_start - section.file_offset)
            for match in _PRINTABLE_RUN.finditer(body):
                total += 1
                if len(rows) < cap:
                    rows.append({
                        "address": _hex(va_base + match.start()),
                        "value": _clean(match.group(0).decode("ascii", "replace"), text_clip),
                    })
                if total >= count_limit:
                    return rows, total
    return rows, total


# ── the answer ──────────────────────────────────────────────────────────────────────────────


def _section_end(sections: list[_Section], address: int) -> int:
    for section in sections:
        if section.size > 0 and section.start <= address < section.start + section.size:
            return section.start + section.size
    return 0


def _finish(image: _Image, data: bytes, caps: Mapping[str, int]) -> dict[str, Any]:
    addrs = sorted(image.functions)
    names = {addr: (_clean(image.functions[addr], 120) or f"sub_{addr:x}") for addr in addrs}
    sizes: dict[int, int] = {}
    for index, addr in enumerate(addrs):
        size = image.sizes.get(addr, 0)
        if size <= 0:
            sec_end = _section_end(image.sections, addr)
            next_addr = addrs[index + 1] if index + 1 < len(addrs) else 0
            if next_addr and sec_end:
                size = min(next_addr, sec_end) - addr
            elif next_addr:
                size = next_addr - addr
            elif sec_end:
                size = sec_end - addr
        sizes[addr] = max(0, size)
    out, incoming = _scan_calls(image, data, addrs, sizes, caps)
    unique_imports: list[tuple[str, str]] = []
    seen_imports: set[tuple[str, str]] = set()
    for name, library in image.imports:
        clean = (_clean(name, 120), _clean(library, 80))
        if clean[0] and clean not in seen_imports:
            seen_imports.add(clean)
            unique_imports.append(clean)
    strings, string_total = _strings(image, data, caps["strings"], caps["text"])
    blocks = [
        {"name": _clean(section.name, 60) or "(unnamed)", "start": _hex(section.start),
         "size": section.size, "perms": section.perms}
        for section in image.sections if section.size > 0
    ]
    rows = [
        {
            "name": names[addr], "address": _hex(addr), "size": sizes[addr], "thunk": False,
            "callers": [names[caller] for caller in sorted(incoming[addr])][: caps["edges"]],
            "callees": [names[callee] for callee in sorted(out[addr])][: caps["edges"]],
        }
        for addr in addrs[: caps["functions"]]
    ]
    return {
        "version": INSPECTOR_VERSION,
        "program": {
            "language": image.language, "compiler": image.compiler, "format": image.format,
            "image_base": _hex(image.image_base), "bits": str(image.bits),
        },
        "functions": rows,
        "imports": [{"name": name, "library": library} for name, library in unique_imports[: caps["imports"]]],
        "strings": strings,
        "blocks": blocks[: caps["blocks"]],
        "totals": {
            "functions": len(addrs), "imports": len(unique_imports),
            "strings": string_total, "blocks": len(blocks),
        },
    }


def inspect(data: bytes, caps: Mapping[str, int]) -> dict[str, Any]:
    """The inventory for one file, or `UnsupportedFormat` if it is not ELF, PE or Mach-O."""
    if data[:4] == b"\x7fELF":
        image = _elf(data)
    elif data[:2] == b"MZ":
        image = _pe(data)
    elif len(data) >= 4 and (
        struct.unpack_from(">I", data, 0)[0] in (0xCAFEBABE, 0xCAFEBABF, 0xFEEDFACE, 0xFEEDFACF)
        or struct.unpack_from("<I", data, 0)[0] in (0xFEEDFACE, 0xFEEDFACF)
    ):
        image = _macho(data)
    else:
        raise UnsupportedFormat("not an ELF, PE or Mach-O file")
    return _finish(image, data, caps)


# ── ELF ─────────────────────────────────────────────────────────────────────────────────────


def _elf(data: bytes) -> _Image:
    if len(data) < 16 or data[:4] != b"\x7fELF":
        raise UnsupportedFormat("not an ELF file")
    elf_class, elf_data = data[4], data[5]
    if elf_class not in (1, 2) or elf_data not in (1, 2):
        raise UnsupportedFormat("an ELF header this reader does not understand")
    endian = "<" if elf_data == 1 else ">"
    bits = 64 if elf_class == 2 else 32
    if bits == 64:
        (_e_type, e_machine, _e_version, _e_entry, _e_phoff, e_shoff, _e_flags,
         _e_ehsize, _e_phentsize, _e_phnum, e_shentsize, e_shnum, e_shstrndx) = _unpack(
            data, endian, "HHIQQQIHHHHHH", 16
        )
        section_format = "IIQQQQIIQQ"
        section_size = 64
    else:
        (_e_type, e_machine, _e_version, _e_entry, _e_phoff, e_shoff, _e_flags,
         _e_ehsize, _e_phentsize, _e_phnum, e_shentsize, e_shnum, e_shstrndx) = _unpack(
            data, endian, "HHIIIIIHHHHHH", 16
        )
        section_format = "IIIIIIIIII"
        section_size = 40
    arch, width = _ELF_MACHINES.get(e_machine, ("unknown", bits))
    headers: list[tuple[int, ...]] = []
    if e_shoff and e_shnum:
        if e_shentsize < section_size:
            raise UnsupportedFormat("ELF section headers are smaller than the format allows")
        _need(data, e_shoff, e_shnum * e_shentsize)
        for index in range(min(e_shnum, MAX_SECTIONS)):
            headers.append(_unpack(data, endian, section_format, e_shoff + index * e_shentsize))
    else:
        raise UnsupportedFormat("an ELF file with no section table")

    names_data = b""
    if 0 <= e_shstrndx < len(headers):
        names_data = _bytes_at(data, headers[e_shstrndx][4], headers[e_shstrndx][5])
    names = [_cstr(names_data, header[0]) for header in headers]
    sections: list[_Section] = []
    for name, header in zip(names, headers, strict=False):
        _sh_name, sh_type, sh_flags, sh_addr, sh_offset, sh_size = header[:6]
        if sh_addr == 0 or sh_size == 0:
            continue
        executable = bool(sh_flags & 0x4)
        sections.append(_Section(
            name=name, start=sh_addr, size=sh_size, file_offset=sh_offset,
            file_size=0 if sh_type == 8 else sh_size,  # SHT_NOBITS has no bytes in the file
            perms=_perms(bool(sh_flags & 0x2), bool(sh_flags & 0x1), executable),
            executable=executable,
        ))
    libraries = _elf_libraries(data, endian, headers, names, bits)
    library = libraries[0] if len(libraries) == 1 else ""
    functions: dict[int, str] = {}
    sizes: dict[int, int] = {}
    imports: list[tuple[str, str]] = []
    for header, _name in zip(headers, names, strict=False):
        sh_type, sh_link, sh_entsize, sh_offset, sh_size = header[1], header[6], header[9], header[4], header[5]
        if sh_type not in (2, 11) or sh_entsize <= 0:
            continue
        if not (0 <= sh_link < len(headers)):
            continue
        strings_data = _bytes_at(data, headers[sh_link][4], headers[sh_link][5])
        entry_size = 24 if bits == 64 else 16
        if sh_entsize < entry_size:
            continue
        for index in range(min(sh_size // sh_entsize, 200_000)):
            offset = sh_offset + index * sh_entsize
            if offset + entry_size > len(data):
                break
            if bits == 64:
                st_name, st_info, _st_other, st_shndx, st_value, st_size = _unpack(data, endian, "IBBHQQ", offset)
            else:
                st_name, st_value, st_size, st_info, _st_other, st_shndx = _unpack(data, endian, "IIIBBH", offset)
            symbol = _clean(_cstr(strings_data, st_name), 120)
            if not symbol:
                continue
            kind = st_info & 0xF
            if st_shndx == 0:
                if kind in (0, 2):
                    imports.append((symbol, library))
            elif kind == 2 and st_value:
                functions.setdefault(st_value, symbol)
                sizes.setdefault(st_value, int(st_size))
    compiler = _elf_compiler(names, headers, data)
    return _Image(
        format="Executable and Linking Format (ELF)", language=_language(arch, width, endian),
        compiler=compiler, bits=width, image_base=min((s.start for s in sections), default=0),
        arch=arch, endian=endian, sections=sections, functions=functions, sizes=sizes, imports=imports,
    )


def _elf_libraries(data: bytes, endian: str, headers: list[tuple[int, ...]], names: list[str], bits: int) -> list[str]:
    """The DT_NEEDED names, so an import can name the library it comes from when there is only one."""
    entry = 16 if bits == 64 else 8
    for header, _name in zip(headers, names, strict=False):
        if header[1] != 6:  # SHT_DYNAMIC
            continue
        sh_link, sh_offset, sh_size = header[6], header[4], header[5]
        if not (0 <= sh_link < len(headers)):
            continue
        strings_data = _bytes_at(data, headers[sh_link][4], headers[sh_link][5])
        found: list[str] = []
        for index in range(min(sh_size // entry, 4096)):
            offset = sh_offset + index * entry
            if offset + entry > len(data):
                break
            if bits == 64:
                tag, value = _unpack(data, endian, "qQ", offset)
            else:
                tag, value = _unpack(data, endian, "iI", offset)
            if tag == 0:
                break
            if tag == 1:
                needed = _cstr(strings_data, int(value))
                if needed:
                    found.append(needed)
        return found
    return []


def _elf_compiler(names: list[str], headers: list[tuple[int, ...]], data: bytes) -> str:
    for name, header in zip(names, headers, strict=False):
        if name != ".comment":
            continue
        sh_type, sh_offset, sh_size = header[1], header[4], header[5]
        if sh_type == 8 or sh_size <= 0:
            continue
        text = _bytes_at(data, sh_offset, min(sh_size, 65_536)).decode("utf-8", "replace")
        if "GCC" in text:
            return "GCC"
        lowered = text.lower()
        if "clang" in lowered:
            return "clang"
    return ""


# ── PE ──────────────────────────────────────────────────────────────────────────────────────


def _pe(data: bytes) -> _Image:
    if data[:2] != b"MZ" or len(data) < 64:
        raise UnsupportedFormat("not a PE file")
    e_lfanew = _unpack(data, "<", "I", 0x3C)[0]
    if _bytes_at(data, e_lfanew, 4) != b"PE\x00\x00":
        raise UnsupportedFormat("not a PE file")
    machine, section_count, _stamp, symbol_ptr, symbol_count, optional_size, _characteristics = _unpack(
        data, "<", "HHIIIHH", e_lfanew + 4
    )
    optional = e_lfanew + 24
    if optional_size < 32:
        raise UnsupportedFormat("a PE optional header shorter than the format allows")
    _need(data, optional, optional_size)
    magic = _unpack(data, "<", "H", optional)[0]
    if magic == 0x10B:
        bits, image_base, entry = 32, _unpack(data, "<", "I", optional + 28)[0], _unpack(data, "<", "I", optional + 16)[0]
        directories = optional + 96
        directory_count = _unpack(data, "<", "I", optional + 92)[0] if optional_size >= 96 else 0
    elif magic == 0x20B:
        bits, image_base, entry = 64, _unpack(data, "<", "Q", optional + 24)[0], _unpack(data, "<", "I", optional + 16)[0]
        directories = optional + 112
        directory_count = _unpack(data, "<", "I", optional + 108)[0] if optional_size >= 112 else 0
    else:
        raise UnsupportedFormat("a PE optional header this reader does not understand")
    arch, width = {(0x14C): ("x86", 32), (0x8664): ("x86", 64), (0xAA64): ("arm64", 64)}.get(
        int(machine), ("unknown", bits)
    )
    sections: list[_Section] = []
    raw_sections: list[tuple[int, int, int, int, int]] = []  # va, vsize, raw offset, raw size, characteristics
    table = optional + optional_size
    _need(data, table, section_count * 40)
    for index in range(min(section_count, MAX_SECTIONS)):
        offset = table + index * 40
        name = data[offset:offset + 8].split(_NULL, 1)[0].decode("utf-8", "replace")
        virtual_size, virtual_address, raw_size, raw_offset = _unpack(data, "<", "IIII", offset + 8)
        characteristics = _unpack(data, "<", "I", offset + 36)[0]
        raw_sections.append((virtual_address, virtual_size, raw_offset, raw_size, characteristics))
        executable = bool(characteristics & 0x20000000)
        read = bool(characteristics & 0x40000000)
        write = bool(characteristics & 0x80000000)
        sections.append(_Section(
            name=name, start=image_base + virtual_address, size=max(virtual_size, raw_size),
            file_offset=raw_offset, file_size=raw_size,
            perms=_perms(read, write, executable), executable=executable,
        ))

    def rva_offset(rva: int) -> int | None:
        for virtual_address, virtual_size, raw_offset, raw_size, _ in raw_sections:
            if virtual_address <= rva < virtual_address + max(virtual_size, raw_size) and rva - virtual_address < raw_size:
                return raw_offset + (rva - virtual_address)
        return None

    def read_rva(rva: int, limit: int = 300) -> str:
        offset = rva_offset(rva)
        return _cstr(data, offset, limit) if offset is not None else ""

    max_directories = max(0, (optional + optional_size - directories) // 8)

    def directory(index: int) -> tuple[int, int]:
        if index >= directory_count or index >= max_directories:
            return 0, 0
        _need(data, directories + index * 8, 8)
        return _unpack(data, "<", "II", directories + index * 8)

    functions: dict[int, str] = {}
    sizes: dict[int, int] = {}
    exports_rva, exports_size = directory(0)
    if exports_rva and exports_size:
        offset = rva_offset(exports_rva)
        if offset is not None and offset + 40 <= len(data):
            count = _unpack(data, "<", "I", offset + 24)[0]
            functions_rva, names_rva, ordinals_rva = _unpack(data, "<", "III", offset + 28)
            function_offset, name_offset, ordinal_offset = (
                rva_offset(functions_rva), rva_offset(names_rva), rva_offset(ordinals_rva)
            )
            if function_offset is not None and name_offset is not None and ordinal_offset is not None:
                for index in range(min(count, 2000)):
                    if name_offset + index * 4 + 4 > len(data) or ordinal_offset + index * 2 + 2 > len(data):
                        break
                    name = read_rva(_unpack(data, "<", "I", name_offset + index * 4)[0], 120)
                    ordinal = _unpack(data, "<", "H", ordinal_offset + index * 2)[0]
                    target = function_offset + ordinal * 4
                    if not name or target + 4 > len(data):
                        continue
                    address = image_base + _unpack(data, "<", "I", target)[0]
                    functions.setdefault(address, name)
                    sizes.setdefault(address, 0)

    imports: list[tuple[str, str]] = []
    imports_rva, imports_size = directory(1)
    if imports_rva and imports_size:
        offset = rva_offset(imports_rva)
        if offset is not None:
            for index in range(min(imports_size // 20, 512)):
                at = offset + index * 20
                if at + 20 > len(data):
                    break
                original, _stamp, _forwarder, name_rva, first_thunk = _unpack(data, "<", "IIIII", at)
                if not (original or name_rva or first_thunk):
                    break
                library = read_rva(name_rva, 120)
                lookup_rva = original or first_thunk
                thunk_offset = rva_offset(lookup_rva) if lookup_rva else None
                if thunk_offset is None:
                    continue
                size = 8 if bits == 64 else 4
                for thunk in range(4096):
                    entry = thunk_offset + thunk * size
                    if entry + size > len(data):
                        break
                    value = _unpack(data, "<", "Q" if bits == 64 else "I", entry)[0]
                    if value == 0:
                        break
                    if bits == 64 and value & (1 << 63):
                        continue
                    if bits == 32 and value & (1 << 31):
                        continue
                    imported = read_rva(int(value) + 2, 200)
                    if imported:
                        imports.append((imported, library))

    # COFF symbols make an object file useful even when it has no export table.
    if symbol_ptr and symbol_count:
        string_table = symbol_ptr + symbol_count * 18
        sym_limit = min(symbol_count, 200_000)
        index = 0
        while index < sym_limit:
            at = symbol_ptr + index * 18
            if at + 18 > len(data):
                break
            raw_name = data[at:at + 8]
            value, section_number, kind, _storage_class, num_aux = _unpack(data, "<", "IhHBB", at + 8)
            if raw_name[:4] == b"\x00\x00\x00\x00":
                str_offset = struct.unpack_from("<I", raw_name, 4)[0]
                if str_offset >= 4 and string_table + str_offset < len(data):
                    name = _cstr(data, string_table + str_offset, 120)
                else:
                    name = ""
            else:
                name = raw_name.split(_NULL, 1)[0].decode("utf-8", "replace")
            if name and 0 < section_number <= len(raw_sections) and kind & 0x20:
                address = image_base + raw_sections[section_number - 1][0] + int(value)
                functions.setdefault(address, name)
                sizes.setdefault(address, 0)
            index += 1 + int(num_aux)

    compiler = ""
    for section in sections:
        if section.name in (".comment", ".rdata", ".data"):
            text = _bytes_at(data, section.file_offset, min(section.file_size, 1_000_000)).decode("utf-8", "replace")
            if "GCC" in text:
                compiler = "GCC"
                break
            if "clang" in text.lower():
                compiler = "clang"
                break
    return _Image(
        format="Portable Executable (PE)", language=_language(arch, width, "<"), compiler=compiler,
        bits=width, image_base=image_base, arch=arch, endian="<", sections=sections,
        functions=functions, sizes=sizes, imports=imports,
    )


# ── Mach-O ──────────────────────────────────────────────────────────────────────────────────


def _macho(data: bytes, _depth: int = 0) -> _Image:
    if len(data) < 4:
        raise UnsupportedFormat("not a Mach-O file")
    magic_be = struct.unpack_from(">I", data, 0)[0]
    magic_le = struct.unpack_from("<I", data, 0)[0]
    if magic_be in (0xCAFEBABE, 0xCAFEBABF) and len(data) >= 8:
        if _depth >= _MAX_FAT_DEPTH:
            raise UnsupportedFormat("a fat Mach-O is nested too deeply")
        count = struct.unpack_from(">I", data, 4)[0]
        if magic_be == 0xCAFEBABF:
            if count == 0 or len(data) < 8 + 32:
                raise UnsupportedFormat("a fat Mach-O with no architecture")
            offset, size = struct.unpack_from(">QQ", data, 16)
        else:
            if count == 0 or len(data) < 8 + 20:
                raise UnsupportedFormat("a fat Mach-O with no architecture")
            offset, size = struct.unpack_from(">II", data, 16)
        if offset <= 0 or size <= 0 or size >= len(data) or offset + size > len(data):
            raise UnsupportedFormat("a fat Mach-O slice is outside the file")
        return _macho(data[offset:offset + size], _depth + 1)
    if magic_le == 0xFEEDFACF:
        endian, bits = "<", 64
    elif magic_le == 0xFEEDFACE:
        endian, bits = "<", 32
    elif magic_be == 0xFEEDFACF:
        endian, bits = ">", 64
    elif magic_be == 0xFEEDFACE:
        endian, bits = ">", 32
    else:
        raise UnsupportedFormat("not a Mach-O file")
    header = 32 if bits == 64 else 28
    _need(data, 0, header)
    cputype, _cpusubtype, filetype, command_count, _sizeof, _flags = _unpack(
        data, endian, "iIIIII", 4
    )
    arch, width = {(0x01000007): ("x86", 64), (0x00000007): ("x86", 32),
                   (0x0100000C): ("arm64", 64), (0x0000000C): ("arm", 32)}.get(
        int(cputype), ("unknown", bits)
    )
    sections: list[_Section] = []
    segments: list[tuple[int, int, int, int]] = []  # vmaddr, vmsize, fileoff, filesize
    functions: dict[int, str] = {}
    sizes: dict[int, int] = {}
    imports: list[tuple[str, str]] = []
    dylibs: list[str] = []
    symoff = stroff = nsyms = 0
    offset = header
    for _ in range(min(command_count, 4096)):
        if offset + 8 > len(data):
            break
        command, command_size = _unpack(data, endian, "II", offset)
        if command_size < 8 or offset + command_size > len(data):
            break
        if command == 0x19:  # LC_SEGMENT_64
            if bits == 64 and command_size >= 72:
                vmaddr, vmsize, fileoff, filesize = _unpack(data, endian, "QQQQ", offset + 24)
                initprot = _unpack(data, endian, "i", offset + 60)[0]
                section_count = _unpack(data, endian, "I", offset + 64)[0]
                section_at = offset + 72
                section_stride = 80
                segments.append((int(vmaddr), int(vmsize), int(fileoff), int(filesize)))
                for index in range(min(section_count, 256)):
                    if len(sections) >= MAX_SECTIONS:
                        break
                    at = section_at + index * section_stride
                    if at + section_stride > offset + command_size or at + section_stride > len(data):
                        break
                    name = data[at:at + 16].split(_NULL, 1)[0].decode("utf-8", "replace")
                    seg = data[at + 16:at + 32].split(_NULL, 1)[0].decode("utf-8", "replace")
                    address, size, file_at = _unpack(data, endian, "QQI", at + 32)
                    sect_flags = _unpack(data, endian, "I", at + 64)[0]
                    sect_type = int(sect_flags) & 0xFF
                    zerofill = sect_type in (0x1, 0xC, 0x12) or int(file_at) == 0
                    readable = bool(int(initprot) & 1)
                    writable = bool(int(initprot) & 2)
                    executable = bool(int(initprot) & 4)
                    sections.append(_Section(
                        name=f"{seg},{name}" if seg else name, start=int(address), size=int(size),
                        file_offset=0 if zerofill else int(file_at),
                        file_size=0 if zerofill else int(size),
                        perms=_perms(readable, writable, executable),
                        executable=executable,
                    ))
        elif command == 0x1:  # LC_SEGMENT (32-bit)
            if bits == 32 and command_size >= 56:
                vmaddr, vmsize, fileoff, filesize = _unpack(data, endian, "IIII", offset + 24)
                initprot = _unpack(data, endian, "i", offset + 44)[0]
                section_count = _unpack(data, endian, "I", offset + 48)[0]
                section_at = offset + 56
                section_stride = 68
                segments.append((int(vmaddr), int(vmsize), int(fileoff), int(filesize)))
                for index in range(min(section_count, 256)):
                    if len(sections) >= MAX_SECTIONS:
                        break
                    at = section_at + index * section_stride
                    if at + section_stride > offset + command_size or at + section_stride > len(data):
                        break
                    name = data[at:at + 16].split(_NULL, 1)[0].decode("utf-8", "replace")
                    seg = data[at + 16:at + 32].split(_NULL, 1)[0].decode("utf-8", "replace")
                    address, size, file_at = _unpack(data, endian, "III", at + 32)
                    sect_flags = _unpack(data, endian, "I", at + 56)[0]
                    sect_type = int(sect_flags) & 0xFF
                    zerofill = sect_type in (0x1, 0xC, 0x12) or int(file_at) == 0
                    readable = bool(int(initprot) & 1)
                    writable = bool(int(initprot) & 2)
                    executable = bool(int(initprot) & 4)
                    sections.append(_Section(
                        name=f"{seg},{name}" if seg else name, start=int(address), size=int(size),
                        file_offset=0 if zerofill else int(file_at),
                        file_size=0 if zerofill else int(size),
                        perms=_perms(readable, writable, executable),
                        executable=executable,
                    ))
        elif command == 0x2:  # LC_SYMTAB
            if command_size >= 24:
                symoff, nsyms, stroff, _strsize = _unpack(data, endian, "IIII", offset + 8)
        elif command in (0xC, 0xD, 0x80000018, 0x8000001F):  # dylib load commands
            if command_size >= 12:
                name_at = int(_unpack(data, endian, "I", offset + 8)[0])
                if 12 <= name_at < command_size:
                    name = _cstr(data[:offset + command_size], offset + name_at, 200)
                    if name:
                        dylibs.append(name.rsplit("/", 1)[-1])
        offset += command_size
    library = dylibs[0] if len(dylibs) == 1 else ""
    if symoff and nsyms:
        entry_size = 16 if bits == 64 else 12
        for index in range(min(int(nsyms), 200_000)):
            at = int(symoff) + index * entry_size
            if at + entry_size > len(data):
                break
            if bits == 64:
                strx, ntype, nsect, _desc, value = _unpack(data, endian, "IBBHQ", at)
            else:
                strx, ntype, nsect, _desc, value = _unpack(data, endian, "IBBHI", at)
            if ntype & 0xE0:  # N_STAB
                continue
            name = _clean(_cstr(data, int(stroff) + int(strx), 120), 120)
            if not name:
                continue
            kind = ntype & 0x0E
            if kind == 0x0E and nsect and value:  # N_SECT
                functions.setdefault(int(value), name)
                sizes.setdefault(int(value), 0)
            elif kind == 0 and not int(value):
                imports.append((name, library))
    compiler = ""
    for section in sections:
        if "comment" in section.name.lower():
            text = _bytes_at(data, section.file_offset, section.file_size).decode("utf-8", "replace")
            if "GCC" in text:
                compiler = "GCC"
            elif "clang" in text.lower():
                compiler = "clang"
    return _Image(
        format=f"Mach-O {bits}-bit {'object' if filetype == 1 else 'executable'}",
        language=_language(arch, width, endian), compiler=compiler, bits=width,
        image_base=min((segment[0] for segment in segments if segment[0]), default=0),
        arch=arch, endian=endian, sections=sections, functions=functions, sizes=sizes, imports=imports,
    )
