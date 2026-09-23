# rmg_permissive - one validated byte

A kernel module that puts SELinux into permissive mode by writing a single byte of kernel **data**
(`struct selinux_state.enforcing`), rather than patching kernel **text**.

```bash
# derive the offset for the kernel being built against, and stamp it
python3 tools/btf_selinux_layout.py path/to/vmlinux        # offset=0 develop=yes via=struct

# build it against a prepared kernel tree (the DDK image exports KDIR)
make -C "$KDIR" M="$PWD/lkm/permissive" modules RMG_ENFORCING_OFFSET=0

# test the rule the write is gated on, on the host, with no kernel involved
make -C lkm/permissive test
```

## Why this exists

`setenforce 0` has to get past two things: the policy must allow the caller to write
`/sys/fs/selinux/enforce`, and the SELinux filesystem must answer honestly. On this device both have
failed for us. Our own `selinux_hide` forge is one of the reasons, and it is also what denies the su
domain the `ctl.*` property writes that KernelSU's own soft reboot needs - so a kernel-side write has
a real use: it works when the policy and every userspace path do not.

It is also the first thing in this project that writes kernel **data** instead of kernel **text**,
which is what makes it possible at all: Samsung's RKP pins kernel text at EL2, which is why our
text-patching `selinux_hide` faults with `-38` on this hardware, while a data byte stays writable.
None of that is our discovery and none of it is claimed below.

## Where the offset comes from

The offset is derived per kernel, never remembered. `tools/btf_selinux_layout.py` reads the kernel's
own type information and answers three things at once:

| | |
|---|---|
| `offset` | the byte to write |
| `develop` | whether that byte is a runtime switch at all - it exists only under `CONFIG_SECURITY_SELINUX_DEVELOP`, and without it the first byte is `initialized` |
| `via` | which BTF record answered (`var` or `struct`), so a receipt is never ambiguous |

Both BTF record kinds are searched because a kernel need not have both. Measured on a Galaxy S25
Ultra (`6.6.98`, android15-6.6): 146,830 types, **no `BTF_KIND_VAR` at all**, so the struct type name
is the only route there.

That device's BTF, dumped member by member:

```
   0  enforcing                  <- byte to write, and it is byte 0 *because* this kernel has the
   1  initialized                   field with nothing ahead of it
   2  policycap                  (8 bytes, ending at byte 10)
  10  android_netlink_route      <- Samsung's own additions, mid-struct
  11  android_netlink_getneigh
  16  status_page
  24  status_lock
  72  policy
  80  policy_mutex
```

Two conclusions. The field is byte 0 here, but the tool is what establishes that per kernel rather
than anyone remembering it. And a vendor struct has members in the middle that the upstream header
does not describe - so the check that guards the write has to be a shape test, not arithmetic.

## The rule the write is gated on

`selinux_layout.h` is compiled by both the kernel module and the host test, so there is one copy of
the rule rather than two that can drift. Before anything is written, the bytes at the candidate
offset must look like:

```
[enforcing: 0 or 1] [initialized: exactly 1] [policycap: 8 bytes, each 0 or 1]
```

`initialized` is set once at policy load and never cleared, so it is 1 on any kernel with a live
policy. The eight bytes after it are the `policycap[]` boolean array; a pointer's bytes land there
when the offset names the wrong field or `CONFIG_RANDSTRUCT` shuffled the layout, and eight pointer
bytes are almost never all 0-1.

**The rule's known limit, asserted in the test so nobody "fixes" it in the wrong direction:** on a
kernel built *without* `CONFIG_SECURITY_SELINUX_DEVELOP`, a layout whose `policycap[0]` is true is
indistinguishable by shape from a DEVELOP one. Only the caller can separate them, and it does it by
refusing to load when `/sys/fs/selinux/enforce` does not exist. That is a precondition, not a
belt-and-braces check.

Verified on the device this was written against:

```
CONFIG_SECURITY_SELINUX_DEVELOP=y      the switch exists
CONFIG_RANDSTRUCT_NONE=y               the layout is not shuffled
CONFIG_DEBUG_INFO_BTF=y                the derivation above is possible
CONFIG_KALLSYMS_ALL=y                  the symbol the module resolves is in the table it walks
-rw-r--r-- /sys/fs/selinux/enforce     the caller's precondition holds
```

## The contract with the caller

The write is the product, and a module that is not in `/proc/modules` is a module no detector can
see. So on success `init` returns `-E2BIG` **on purpose**: the kernel does not finish loading it,
nothing is left behind, and the write has already happened.

```
rmg_permissive: result=applied offset=0 before=1 after=0 symbol=selinux_state value=0
```

- `result=applied|noop|dry-run` - what happened. Read this line, not the errno, to know.
- `result=refused reason=...` - nothing was written: `no-offset`, `offset-out-of-window`,
  `value-not-a-bool`, `no-symbol`, or `layout-mismatch` (which prints the four bytes it saw).
- `result=verify-failed` - the byte changed and the shape no longer holds, meaning `initialized` was
  not where the rule expected it. Impossible by construction, which is why it is checked.

`stay=1` returns success and keeps the module loaded for a caller that would rather see
`init_module` return 0; only then is there a `module_exit`.

| parameter | default | meaning |
|---|---|---|
| `offset` | the CI stamp | byte offset of the field; `-1` refuses to write |
| `value` | `0` | `0` permissive, `1` enforcing |
| `dry_run` | `false` | check the layout and report, write nothing |
| `stay` | `false` | stay loaded instead of returning `-E2BIG` |
| `symbol` | `selinux_state` | symbol to write into |

Every parameter is `0400`, so they are reachable at load and not afterwards.

## Credits and licensing

- **The technique is not ours.** Writing a data field instead of patching kernel text, and returning
  an error from `init` so the module leaves nothing in `/proc/modules`, are both ideas this module
  took from [DFReroot](https://github.com/polygraphene/DFReroot) (polygraphene), whose
  `dirtyfrag-lkm/dirtyfrag.c` does the same thing for the same reason. That repository carries **no
  license at all**, so none of its source was copied, adapted or vendored here - this file was
  written from the kernel's own headers (`security/selinux/include/security.h`, `kernel/kallsyms.c`,
  `include/linux/kallsyms.h`) and from the public documentation of `struct selinux_state`. The
  design difference is deliberate and is the reason this is a separate module rather than a copy: the
  offset is derived per kernel and the write is gated on a shape test, where a fixed "first byte"
  assumption is only correct by accident of config.
- **`kallsyms_on_each_symbol`** (`EXPORT_SYMBOL_GPL`) is the kernel's own supported way for a module
  to resolve an unexported symbol by name; it is why this module needs no kprobe trick.
- **This file and `rmg_permissive.c` are `GPL-2.0`**, unlike the rest of this repository, which is
  Apache-2.0. A module that binds against `EXPORT_SYMBOL_GPL` symbols has to be GPL-compatible; the
  SPDX header on each file is the record of that, and of which files it covers.

## What is not verified

- **Not built or loaded yet.** There is no kernel tree on the development machine, so the module has
  only ever been compiled by CI and never loaded on the device. `make test` covers the rule, not the
  module.
- **The symbol lookup is unproven on hardware.** `kallsyms_on_each_symbol` reads the kernel's own
  tables, which is not the same thing as `/proc/kallsyms`: on this device a shell uid gets
  `Permission denied` on that file, while the module walks the table directly. That the walk finds
  `selinux_state` is expected from `CONFIG_KALLSYMS_ALL=y` and has not been observed.
- **A false refusal is possible on another kernel** whose `policycap[]` is shorter than eight bytes:
  the tail test would then read into `status_page` and refuse. That is the safe direction, and the
  fix if it is ever met is a bound parameter rather than a weaker rule.
- **No route to the device yet.** CI publishes the `.ko` as an artifact; nothing stages it, loads it,
  or checks the result, and the feed has no entry for it. That wiring is the next step.
