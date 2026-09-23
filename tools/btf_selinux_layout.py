#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Derive the byte offset of `struct selinux_state.enforcing` from a kernel's own type information.

The module in lkm/permissive/ writes that byte to switch SELinux to permissive. The offset is not
constant, and guessing it writes into whatever field really lives there:

  * `enforcing` exists only when CONFIG_SECURITY_SELINUX_DEVELOP is on; without it the first byte is
    `initialized`, and clearing that tells the kernel SELinux was never initialised;
  * `struct selinux_state` is `__randomize_layout`, so under CONFIG_RANDSTRUCT the field order is
    shuffled;
  * a vendor kernel may carry its own additions ahead of the field.

BTF settles all three from the running kernel's own types, and it is present where it matters:
CONFIG_DEBUG_INFO_BTF=y puts `struct selinux_state` (member names, order and bit offsets) in
/sys/kernel/btf/vmlinux - readable by root, which is exactly who loads the module, though a Samsung
kernel refuses it to the shell uid - and the DDK's vmlinux carries the same section for the tree a
module is built against.

So this tool answers three things at once, and refuses rather than guesses when it cannot:

    offset=0          the byte to write
    develop=yes|no    whether that byte is a runtime switch at all - it is the presence of the
                      `enforcing` member, which is what the #ifdef above decides
    via=struct|var    which kind of BTF record answered, so a receipt can be read years later

Used by .github/workflows/lkm-build.yml to stamp RMG_ENFORCING_OFFSET for the tree a module is built
against, and usable by hand against a device's /sys/kernel/btf/vmlinux, which is how a caller can
derive the offset for a kernel whose stamp is wrong.

Both record kinds are searched, because a kernel need not have both. Measured on a Galaxy S25 Ultra
(6.6.98, android15-6.6): 146,830 types, and the BTF carries **no BTF_KIND_VAR at all** - so
`selinux_state` cannot be found as a variable there and the struct type name is the only route. The
symbol's *address* is a different question and is not asked here: the module resolves that through
kallsyms_on_each_symbol, which reads the kernel's own tables and needs CONFIG_KALLSYMS_ALL=y rather
than anything readable from userspace.

What that device's BTF shows about the struct - which is why the shape rule in selinux_layout.h
reads the bytes it does:

    struct selinux_state @ type 23157, size 88
      enforcing                bit 0     <- CONFIG_SECURITY_SELINUX_DEVELOP is on
      initialized              bit 8
      policycap                bit 16    <- 8 bytes, ending at byte 10
      android_netlink_route    bit 80    <- Samsung's own addition
      android_netlink_getneigh bit 88
      status_page              bit 128
      status_lock              bit 192
      policy                   bit 576
      policy_mutex             bit 640

Two things to take from that. The field the module writes is byte 0, but it is byte 0 *because* that
kernel has the field and no vendor addition ahead of it - the tool is what establishes that, per
kernel, rather than anyone remembering it. And the vendor additions sit in the middle of the struct,
which is why the rule tests shape, not arithmetic.

    python3 tools/btf_selinux_layout.py --self-test
    python3 tools/btf_selinux_layout.py --btf /sys/kernel/btf/vmlinux
    python3 tools/btf_selinux_layout.py path/to/vmlinux          # scans an ELF for its .BTF
    python3 tools/btf_selinux_layout.py --btf f --dump           # every member, in order

Exit codes: 0 found, 3 the member (or the struct) is not in this kernel - which for `enforcing` is
the answer "DEVELOP is off, there is nothing to write" - and 2 for anything that could not be read
or parsed. A caller must not treat 2 as 3: "I could not tell" is not "there is nothing there".
"""

from __future__ import annotations

import argparse
import struct
import sys

MAGIC = 0xEB9F
HEADER = struct.Struct("<HBBIIIII")  # magic, version, flags, hdr_len, type_off, type_len, str_off, str_len

(KIND_INT, KIND_PTR, KIND_ARRAY, KIND_STRUCT, KIND_UNION, KIND_ENUM, KIND_FWD, KIND_TYPEDEF,
 KIND_VOLATILE, KIND_CONST, KIND_RESTRICT, KIND_FUNC, KIND_FUNC_PROTO, KIND_VAR, KIND_DATASEC,
 KIND_FLOAT, KIND_DECL_TAG, KIND_TYPE_TAG, KIND_ENUM64) = range(1, 20)

KIND_NAMES = {KIND_INT: "INT", KIND_PTR: "PTR", KIND_ARRAY: "ARRAY", KIND_STRUCT: "STRUCT",
              KIND_UNION: "UNION", KIND_ENUM: "ENUM", KIND_FWD: "FWD", KIND_TYPEDEF: "TYPEDEF",
              KIND_VOLATILE: "VOLATILE", KIND_CONST: "CONST", KIND_RESTRICT: "RESTRICT",
              KIND_FUNC: "FUNC", KIND_FUNC_PROTO: "FUNC_PROTO", KIND_VAR: "VAR",
              KIND_DATASEC: "DATASEC", KIND_FLOAT: "FLOAT", KIND_DECL_TAG: "DECL_TAG",
              KIND_TYPE_TAG: "TYPE_TAG", KIND_ENUM64: "ENUM64"}

# Bytes each kind puts after its btf_type record, as a function of its vlen.
MEMBERS = struct.Struct("<III")  # name_off, type, offset (bits, or bitfield offset+size)
ARRAY = struct.Struct("<III")
FUNC_PARAM = struct.Struct("<II")
VAR = struct.Struct("<I")
SECINFO = struct.Struct("<III")

EXIT_NOT_FOUND = 3
EXIT_UNREADABLE = 2


class BtfError(Exception):
    """The blob is not BTF this tool can read. Never a substitute for 'not found'."""


class Btf:
    def __init__(self, blob: bytes):
        if len(blob) < HEADER.size:
            raise BtfError(f"only {len(blob)} bytes, too short for a BTF header")

        magic, version, flags, hdr_len, type_off, type_len, str_off, str_len = HEADER.unpack_from(blob, 0)
        if magic != MAGIC:
            raise BtfError(f"magic is 0x{magic:04x}, expected 0x{MAGIC:04x}")
        if version != 1:
            raise BtfError(f"BTF version {version}, expected 1")
        if hdr_len < HEADER.size:
            raise BtfError(f"hdr_len {hdr_len} is smaller than the header itself")
        if hdr_len + type_off + type_len > len(blob) or hdr_len + str_off + str_len > len(blob):
            raise BtfError("the type or string section runs past the end of the blob")
        if type_len == 0:
            raise BtfError("the type section is empty")

        self._types = blob[hdr_len + type_off:hdr_len + type_off + type_len]
        self._strings = blob[hdr_len + str_off:hdr_len + str_off + str_len]
        self._index: list[tuple[int, int, int, int, int]] = []
        self._parse()

    def _parse(self) -> None:
        off = 0
        while off < len(self._types):
            if off + 12 > len(self._types):
                raise BtfError(f"type record at {off} is truncated")

            name_off, info, size_or_type = struct.unpack_from("<III", self._types, off)
            kind = (info >> 24) & 0x1F
            vlen = info & 0xFFFF
            start = off + 12

            if kind in (KIND_INT, KIND_DECL_TAG):
                extra = 4
            elif kind == KIND_ARRAY:
                extra = ARRAY.size
            elif kind in (KIND_STRUCT, KIND_UNION):
                extra = MEMBERS.size * vlen
            elif kind in (KIND_ENUM, KIND_FUNC_PROTO):
                extra = 8 * vlen
            elif kind == KIND_ENUM64:
                extra = 12 * vlen
            elif kind == KIND_VAR:
                extra = VAR.size
            elif kind == KIND_DATASEC:
                extra = SECINFO.size * vlen
            else:
                extra = 0

            if start + extra > len(self._types):
                raise BtfError(f"type {len(self._index) + 1} runs past the type section")

            self._index.append((kind, name_off, vlen, size_or_type, start))
            off = start + extra

    def name(self, name_off: int) -> str:
        if name_off >= len(self._strings):
            raise BtfError(f"string offset {name_off} is outside the string section")
        end = self._strings.find(b"\0", name_off)
        if end < 0:
            raise BtfError(f"string at {name_off} is not terminated")
        return self._strings[name_off:end].decode("utf-8", "replace")

    def record(self, type_id: int) -> tuple[int, int, int, int, int]:
        if type_id < 1 or type_id > len(self._index):
            raise BtfError(f"type id {type_id} is outside 1..{len(self._index)}")
        return self._index[type_id - 1]

    def find_var(self, name: str) -> int:
        """Type id of the type the variable `name` points at, or 0 when there is no such VAR."""
        for type_id, (kind, name_off, _vlen, _sot, _start) in enumerate(self._index, start=1):
            if kind == KIND_VAR and self.name(name_off) == name:
                return self.record(type_id)[3]
        return 0

    def find_type(self, name: str, kinds: tuple[int, ...]) -> int:
        """Type id of the first type named `name` whose kind is one of `kinds`, or 0."""
        for type_id, (kind, name_off, _vlen, _sot, _start) in enumerate(self._index, start=1):
            if kind in kinds and self.name(name_off) == name:
                return type_id
        return 0

    def find_struct(self, name: str) -> tuple[int, str]:
        """The struct or union called `name`, and which way it was reached.

        The variable first: when BTF has BTF_KIND_VAR, the name on the variable is the symbol name
        and is the stronger statement. Falling back to the type name is not a guess about the
        layout - a struct's members are its layout - but it does say which record answered, so a
        receipt is not ambiguous about what was read.
        """
        via_var = self.find_var(name)
        if via_var and self.record(via_var)[0] in (KIND_STRUCT, KIND_UNION):
            return via_var, "var"

        direct = self.find_type(name, (KIND_STRUCT, KIND_UNION))
        if direct:
            return direct, "struct"

        return 0, "none"

    def members(self, type_id: int) -> list[tuple[str, int]]:
        """(name, offset in bits) for a struct or union, in declaration order."""
        kind, _name_off, vlen, _sot, start = self.record(type_id)
        if kind not in (KIND_STRUCT, KIND_UNION):
            raise BtfError(f"type {type_id} is {KIND_NAMES.get(kind, kind)}, not a struct")

        out = []
        for i in range(vlen):
            name_off, _member_type, bit_off = MEMBERS.unpack_from(self._types, start + i * MEMBERS.size)
            out.append((self.name(name_off), bit_off))
        return out


def find_btf_blob(data: bytes) -> bytes:
    """The BTF section inside an ELF, or `data` itself when it is already a bare BTF blob.

    vmlinux is ELF and its BTF lives in a section; /sys/kernel/btf/vmlinux is the raw blob. Rather
    than carry an ELF reader for one section, this scans for the header magic and then *validates*
    the header it found - offsets inside the buffer, version 1, a non-empty type section. A false hit
    that survives that is possible in principle and is why the parse is checked before it is
    trusted; a wrong answer here is a refused write, not a bad one.
    """
    try:
        Btf(data)
        return data
    except BtfError:
        pass

    magic = struct.pack("<H", MAGIC) + b"\x01"
    at = data.find(magic)
    while at >= 0:
        candidate = data[at:]
        try:
            btf = Btf(candidate)
        except BtfError:
            at = data.find(magic, at + 1)
            continue
        if btf.find_struct("selinux_state")[0]:
            return candidate
        at = data.find(magic, at + 1)

    raise BtfError("no BTF header found in this file")


def layout(blob: bytes, symbol: str, field: str, dump: bool) -> int:
    btf = Btf(find_btf_blob(blob))
    struct_id, via = btf.find_struct(symbol)

    if not struct_id:
        print(f"not found: no struct (or variable) named {symbol} in this kernel's BTF", file=sys.stderr)
        return EXIT_NOT_FOUND

    entries = btf.members(struct_id)
    if dump:
        for name, bit_off in entries:
            print(f"  {bit_off // 8:>4}  {name}")

    for name, bit_off in entries:
        if name != field:
            continue
        if bit_off % 8:
            # A bitfield cannot be `enforcing`, which is a plain bool.
            print(f"not found: {symbol}.{field} is a bitfield at bit {bit_off}", file=sys.stderr)
            return EXIT_NOT_FOUND
        print(f"symbol={symbol}")
        print(f"field={field}")
        print(f"offset={bit_off // 8}")
        print("develop=yes")
        print(f"via={via}")
        return 0

    # No `enforcing` member at all is the answer "CONFIG_SECURITY_SELINUX_DEVELOP is off": the field
    # is compiled out, so there is no runtime switch to write and the first byte is `initialized`.
    print(f"not found: {symbol} has no member {field}", file=sys.stderr)
    print(f"{symbol}.first_member={entries[0][0] if entries else '?'}", file=sys.stderr)
    print("develop=no", file=sys.stderr)
    return EXIT_NOT_FOUND


# --------------------------------------------------------------------------------------------
# self-test: a synthetic blob, encoded here, so the parser is checked without a kernel to hand
# --------------------------------------------------------------------------------------------


def _type(kind: int, name_off: int, size_or_type: int, vlen: int = 0, extra: bytes = b"") -> bytes:
    info = (kind << 24) | vlen
    return struct.pack("<III", name_off, info, size_or_type) + extra


def _btf(types: bytes, strings: bytes) -> bytes:
    return HEADER.pack(MAGIC, 1, 0, HEADER.size, 0, len(types), len(types), len(strings)) + types + strings


def _strings(names: list[str]) -> tuple[bytes, dict[str, int]]:
    blob = b"\0"
    offs = {}
    for name in names:
        offs[name] = len(blob)
        blob += name.encode() + b"\0"
    return blob, offs


def self_test() -> int:
    checks = 0
    failures = 0

    def expect(what: str, got: object, want: object) -> None:
        nonlocal checks, failures
        checks += 1
        if got == want:
            print(f"ok   {what}")
        else:
            failures += 1
            print(f"FAIL {what}: got {got!r}, want {want!r}")

    # A struct whose `enforcing` member sits at byte 0, as a DEVELOP kernel has it.
    names = ["selinux_state", "enforcing", "initialized", "policycap", "bool"]
    strings, off = _strings(names)
    int_bool = _type(KIND_INT, off["bool"], 1, extra=struct.pack("<I", 0))
    members = (MEMBERS.pack(off["enforcing"], 1, 0)
               + MEMBERS.pack(off["initialized"], 1, 8)
               + MEMBERS.pack(off["policycap"], 2, 16))
    struct_id = 2
    array_id = 3
    struct_dev = _type(KIND_STRUCT, off["selinux_state"], 32, vlen=3, extra=members)
    array = _type(KIND_ARRAY, 0, 0, extra=ARRAY.pack(1, 1, 9))
    var = _type(KIND_VAR, off["selinux_state"], struct_id, extra=VAR.pack(0))

    types = int_bool + struct_dev + array + var
    blob = _btf(types, strings)

    parsed = Btf(blob)
    expect("struct is found, and the variable route is reported", parsed.find_struct("selinux_state"),
           (struct_id, "var"))
    expect("members come back in order",
           [name for name, _bit in parsed.members(struct_id)], ["enforcing", "initialized", "policycap"])
    expect("bit offsets are in bits", parsed.members(struct_id)[1], ("initialized", 8))
    expect("offset 0 is derived", layout(blob, "selinux_state", "enforcing", False), 0)

    # The same struct without the field: DEVELOP off, which must be a distinct answer from an
    # unreadable blob.
    names = ["selinux_state", "initialized", "policycap", "bool"]
    strings, off = _strings(names)
    int_bool = _type(KIND_INT, off["bool"], 1, extra=struct.pack("<I", 0))
    members = MEMBERS.pack(off["initialized"], 1, 0) + MEMBERS.pack(off["policycap"], 2, 8)
    struct_nodev = _type(KIND_STRUCT, off["selinux_state"], 32, vlen=2, extra=members)
    array = _type(KIND_ARRAY, 0, 0, extra=ARRAY.pack(1, 1, 9))
    var = _type(KIND_VAR, off["selinux_state"], 2, extra=VAR.pack(0))
    nodev = _btf(int_bool + struct_nodev + array + var, strings)
    expect("a struct without the field is 'not found', not a wrong offset",
           layout(nodev, "selinux_state", "enforcing", False), EXIT_NOT_FOUND)

    # A field that is not at byte 0: the offset has to follow the type, not a constant.
    names = ["selinux_state", "enforcing", "initialized", "policycap", "bool"]
    strings, off = _strings(names)
    int_bool = _type(KIND_INT, off["bool"], 1, extra=struct.pack("<I", 0))
    members = (MEMBERS.pack(off["enforcing"], 1, 64)          # byte 8
               + MEMBERS.pack(off["initialized"], 1, 72)
               + MEMBERS.pack(off["policycap"], 2, 80))
    struct_shifted = _type(KIND_STRUCT, off["selinux_state"], 48, vlen=3, extra=members)
    array = _type(KIND_ARRAY, 0, 0, extra=ARRAY.pack(1, 1, 9))
    var = _type(KIND_VAR, off["selinux_state"], 2, extra=VAR.pack(0))
    shifted = _btf(int_bool + struct_shifted + array + var, strings)
    expect("a shifted field is found, wherever it sits", layout(shifted, "selinux_state", "enforcing", False), 0)

    parsed_shifted = Btf(shifted)
    expect("the shifted member's bit offset is 64",
           dict(parsed_shifted.members(2))["enforcing"], 64)

    # A bitfield is not a bool field, and must not be handed to the module.
    names = ["selinux_state", "enforcing", "bool"]
    strings, off = _strings(names)
    int_bool = _type(KIND_INT, off["bool"], 1, extra=struct.pack("<I", 0))
    members = MEMBERS.pack(off["enforcing"], 1, 3)
    struct_bits = _type(KIND_STRUCT, off["selinux_state"], 8, vlen=1, extra=members)
    var = _type(KIND_VAR, off["selinux_state"], 2, extra=VAR.pack(0))
    bits = _btf(int_bool + struct_bits + var, strings)
    expect("a bitfield is refused", layout(bits, "selinux_state", "enforcing", False), EXIT_NOT_FOUND)

    # Garbage must be an error, never a "not found": those two answers drive opposite decisions.
    for what, bad in (("empty", b""),
                      ("wrong magic", b"\x00\x00\x01\x00" + b"\x00" * 20),
                      ("section past the end", HEADER.pack(MAGIC, 1, 0, 24, 0, 4096, 0, 4096) + b"\x00" * 8),
                      ("truncated type record", _btf(b"\x00" * 6, b"\0"))):
        try:
            Btf(find_btf_blob(bad))
        except BtfError:
            expect(f"{what} is refused as unparseable", True, True)
        else:
            expect(f"{what} is refused as unparseable", False, True)

    # The magic scan has to find a BTF section inside a larger file, which is what vmlinux is.
    padded = b"\x7fELF" + b"\x00" * 4092 + blob + b"\x00" * 64
    expect("BTF is found inside a larger file", layout(padded, "selinux_state", "enforcing", False), 0)

    # A kernel whose BTF has the struct but no VARs at all - which is what the S25 Ultra on the
    # bench actually has - must still answer, and must say which record answered.
    names = ["selinux_state", "enforcing", "initialized", "policycap", "bool"]
    strings, off = _strings(names)
    int_bool = _type(KIND_INT, off["bool"], 1, extra=struct.pack("<I", 0))
    members = (MEMBERS.pack(off["enforcing"], 1, 0)
               + MEMBERS.pack(off["initialized"], 1, 8)
               + MEMBERS.pack(off["policycap"], 2, 16))
    struct_only = _btf(int_bool + _type(KIND_STRUCT, off["selinux_state"], 32, vlen=3, extra=members)
                       + _type(KIND_ARRAY, 0, 0, extra=ARRAY.pack(1, 1, 9)), strings)
    expect("a struct with no variable beside it is still found, via the type",
           Btf(struct_only).find_struct("selinux_state"), (2, "struct"))
    expect("and the offset is derived from it",
           layout(struct_only, "selinux_state", "enforcing", False), 0)

    expect("a name that is nowhere in the blob is not found",
           Btf(blob).find_struct("no_such_struct"), (0, "none"))

    print(f"\n{checks} checks, {failures} failures")
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", nargs="?", help="a vmlinux (its .BTF is scanned for) or a bare BTF blob")
    parser.add_argument("--btf", help="a bare BTF blob, e.g. /sys/kernel/btf/vmlinux")
    parser.add_argument("--symbol", default="selinux_state")
    parser.add_argument("--field", default="enforcing")
    parser.add_argument("--dump", action="store_true", help="print every member before answering")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    path = args.btf or args.path
    if not path:
        parser.error("give a path, --btf, or --self-test")

    try:
        with open(path, "rb") as handle:
            blob = handle.read()
    except OSError as error:
        print(f"cannot read {path}: {error}", file=sys.stderr)
        return EXIT_UNREADABLE

    try:
        return layout(blob, args.symbol, args.field, args.dump)
    except BtfError as error:
        print(f"cannot parse BTF in {path}: {error}", file=sys.stderr)
        return EXIT_UNREADABLE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
