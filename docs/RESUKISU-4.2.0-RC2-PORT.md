# ReSukiSU `v4.2.0-rc2`: the Samsung delta for a third flavour

`kernelsu/patches/ReSukiSU-v4.2.0-rc2-samsung-kdp-rkp-defex.patch` (1202 lines, 15 files) is what
lets this flavour's module and daemon run on a stock Galaxy kernel. It is a *port*, not a rebase: the
first delta for a tree we had not carried before, written by reading ReSukiSU and our own KernelSU-Next
delta side by side.

## Why `v4.2.0-rc2`

Because the file layout decides how much of the delta can be reused. ReSukiSU `v4.1.0` - the newest
stable tag - still has the old flat `kernel/` layout (`kernel/ksu.c`, `kernel/syscall_hook_manager.c`),
which is a different tree to patch in every file. `v4.2.0-rc2` reorganized to `kernel/core`,
`kernel/hook`, `kernel/feature`, `kernel/policy`, which is the same shape tiann `v3.3.0` and
KernelSU-Next `v3.4.0` use - so the port is mostly ours, arriving in files that already exist here
under the names our patch expects.

The cost of that choice is the pin itself: there is no stable `v4.2.0` yet, so this flavour claims
support for a release candidate and the upstream watcher has to move it when 4.2.0 ships.

## What the delta does

1. **A refused table write no longer installs a dead dispatcher.** `ksu_syscall_table_hook` returns an
   errno, and `ksu_syscall_hook_init` checks it: on failure it warns, sets `ksu_dispatcher_nr = -1` and
   returns. ReSukiSU's tracepoint handler returns early on a negative dispatcher number, so the
   redirect never happens. Without this, the syscall number of a marked process is rewritten into the
   dispatcher slot while the slot still holds a `ni_syscall` - every hooked call answers `-ENOSYS`,
   which on a phone reads as a root solution that half works. This is the one piece with no
   counterpart in ReSukiSU, and the reason the rest is worth carrying.
2. **The dispatcher-unavailable branch.** `ksu_syscall_hook_manager_init` gains an early branch: the
   Samsung kretprobes are registered and the tracepoint is not. Its tail calls `ksu_setuid_hook_init()`
   and `ksu_sucompat_init()` unconditionally, so on that path they are called only when the stand-ins
   are not compiled - each stand-in initialises its own half. The exit mirrors it exactly.
3. **The stand-ins themselves** (298 lines, from our Next delta): kretprobes on `execve`,
   `newfstatat`, `faccessat`/`faccessat2`, `statx` calling the same handlers the dispatcher would have
   called, and a kretprobe on `setresuid` deferring the work through `task_work` so it runs in the
   caller's context. Every external symbol it uses was checked to exist in this tree; the only
   unresolved name it mentions is the struct it defines itself.
4. **Samsung KDP and DEFEX** (`kernel/compat/samsung_kdp.c`, `samsung_defex.c`, `ksu_samsung_kdp.h`,
   lifted verbatim - they are ours and depend on nothing flavour-specific), wired into `core/init.c`:
   the symbol resolver first, KDP before the credentials are prepared, DEFEX before any hook is
   installed, and everything unwound in reverse at exit. `escape_with_root_profile` installs
   credentials through KDP instead of `commit_creds()` and re-syncs DEFEX after; `tp_marker` releases
   them through `ksu_put_cred`.
5. **`ksu_patch_text` refuses under `CONFIG_KSU_SAMSUNG_NO_PATCH_TEXT`** with `-EOPNOTSUPP`, so callers
   fail with an error instead of half-succeeding against protected memory.
6. **The daemon stages itself before the module is loaded.** `install()` is split into
   `stage_daemon()` and `finish_install()`, with `stage_daemon_from()` for a copy placed at
   `/data/local/tmp/.ksud-stage` by a process that ran before the daemon had a shell; `late-load` uses
   that path and no longer daemonises, because its caller waits for it to finish. This is the same
   change as in the Next delta, and it is what makes the install steps possible at all when the module
   arrives after boot.
7. **`CONFIG_KSU_SAMSUNG_{KDP,RKP,DEFEX,NO_PATCH_TEXT}`** in `Kconfig`, and the `Kbuild` mapping that
   turns them into `-DCONFIG_...=1`. CI passes them as make variables
   (`CONFIG_KSU=m CONFIG_KSU_SAMSUNG_KDP=y ... make`), the way the module jobs already do for the other
   two flavours.

## Deliberately not ported

- **`avc_spoof`, in every form.** ReSukiSU has no such feature: its SELinux story is `selinux_hide`,
  which is the one whose `patch_text` writes a Samsung kernel refuses. So a ReSukiSU flavour ships
  without the AVC-leak mitigation that KernelSU-Next gets, and the `-38` from "hide SELinux
  modifications" is a "this device cannot do that" here rather than something the delta could fix.
- **The `ksu_sucompat_exit` signature change.** Next needed it because its branch *replaces* the tail
  that calls it; here the tail is called and the symbol stays as it is.
- **`ksu_avc_spoof_late_init()` in the late-load branch** - no `avc_spoof` to arm.
- **x86_64's `ksu_syscall_table_hook`**, whose definition is still `void` while the header now declares
  `int`. Inert for this project - every target we build is arm64 - and left visible rather than fixed in
  a file we never compile.

## What the guard says

`tools/check_rkp_branch.py --tree <patched tree>` exits 0 with three notes it prints for the record:
the early-return branch makes 4 calls and registers none of the dispatcher's registrations (intended),
`on_boot_completed()` calls `ksu_selinux_hide_drop_backup_if_unused()`, which the late-load branch
does not, and the setuid kretprobe hands the two uids over in the order this tree declares. The second
is a cleanup rather than a feature arming - unlike the `avc_spoof` call whose absence the same check
found in KernelSU-Next - so it is left alone and stated here instead.

The third check is the one the device run below added. It resolves the order out of the tree - reading
the entry point's parameter names, and following a wrapper's forward call when the names do not say -
so it passes on KernelSU's and KernelSU-Next's `(old_uid, new_uid)` as well as on this tree's legacy
`(ruid, euid, suid)` wrapper, and fails only on the mismatch. It runs in all four build legs, and the
self-test holds it to the ReSukiSU-shaped tree with the other convention's call.

## What the first compile found

Four things, all of them properties of this tree rather than of the delta, and none of them visible
from reading patch against patch:

1. **`ksu_su_compat_enabled` is a `struct static_key_true` here.** KernelSU-Next declares it as a
   plain `bool`, so the ported Samsung block's `a && b` on it was an error rather than a false. The
   block now writes both forms under `KSU_COMPAT_USE_STATIC_KEY` - the guard `feature/sucompat.c` puts
   around every use of the flag - and takes the uid through `ksu_get_uid_t`, which is what this
   tree's own sucompat path does.
2. **`ksu_sucompat_exit` is `__exit` here.** The Samsung kprobe unwind called it on the failure path,
   which is a section mismatch modpost refuses outright (`.init.text` reaching `.exit.text`) - and
   the wrong thing to do besides: it unregisters the `su_compat` feature handler that the rest of the
   module registers its syscall paths against, on a load that only failed to register a kprobe. The
   unwind now unregisters what it registered and returns; `core/init.c` is where this flavour exits
   it. KernelSU-Next's identical block keeps its call, because there the symbol is not `__exit`.
3. **`ksud` is nightly by construction.** `userspace/ksud/src/main.rs` opens with
   `#![feature(decl_macro)]`, which stable rejects with E0554, and ReSukiSU's own `ksud.yml` installs
   nightly with `rust-src`. The toolchain is now a per-flavour input of `ksu-build.yml`.
4. **There is no "latest release" to watch.** Every ReSukiSU release is marked pre-release, so
   GitHub answers `release not found` for the repository and `gh release view` fails; the upstream
   watcher falls back to the newest tag for a flavour in that state. The same fact puts a pre-release
   tag in `git describe`, so `tools/check_pair_version.py` had to learn that `4.2.0-rc2` is a version
   name while `1a879d6a` is not.

What the build proves: both module jobs (patch-text and no-patch-text), `ksud`, the pair module and
`ksud` around it all compile, and both halves report the same version -
`pair version 35144 (4.2.0-rc2) at 3576e6a5255f`, which is `30000 + 700 + 3514` commits. The published
module's `vermagic` is the target's own release, which is what the device's loader checks.

## What the device found: the manager was never crowned

The first run on the `pa3q` target loaded the module, and `su` worked - but ReSukiSU's own manager
reported the kernel as absent: `KernelSU: -1`, `ReSukiSU:` empty, `LKM: false` in its own bug report,
and `could not retrieve kernelsu driver fd` in its log after `install syscall was blocked by seccomp`.
Both of those are the same absence: the manager's process holds no `ksu_driver` fd, which is the only
thing its version, features and module list are read through.

The kernel's side of the story is in `dmesg`, and it is not what it looks like. The manager search ran
and the app was crowned twice - `Crowning manager: com.resukisu.resukisu uid=10553,
signature_index=0` - so recognition was fine. What never happened is the step after it:
`ksu_install_fd()` is called from exactly two places, and the one that serves an app is the manager
branch of `ksu_handle_setuid()`. `install fd for ksu manager(uid=...)` appears nowhere in any of the
three `dmesg` captures. Every app spawn reached the hook - the log is full of
`handle_setresuid from 10553 to 0`-shaped lines - and every one of them was read as a move *to* uid 0.

That is this delta's own bug, and it is a one-line one. KernelSU and KernelSU-Next define
`ksu_handle_setresuid(uid_t old_uid, uid_t new_uid)` and their bridge calls it `(captured_old, current)`,
which is what the ported kretprobe was copied from. This tree kept the older
`ksu_handle_setresuid(uid_t ruid, uid_t euid, uid_t suid)`: the manual-hook entry point, whose `ruid`
is the uid being moved *to* and whose body reads the uid being left from `current_cred()` itself - its
own bridge says so, calling `ksu_handle_setuid(current_uid(), old_uid)`. Handing its first parameter the
*old* uid therefore reverses the pair at every spawn, and `ksu_handle_setuid()` never sees the uid the
process moved to. `ksu_is_manager_uid(new_uid)` is asked about `0`, the manager branch is skipped, and
no manager is ever handed the fd.

The quiet part is the collateral: the same branch is where `ksu_seccomp_allow_cache(filter,
__NR_reboot)` runs, which is how a manager is allowed to install its own fd with the `reboot` syscall -
the exact syscall the manager's log said was blocked. So both routes to a manager fd were closed by the
same hand-off. This was never ReSukiSU-specific in shape either: the same call sits in the KernelSU
`v3.2.5` and `v3.3.0` and KernelSU-Next `v3.3.0` and `v3.4.0` patches, correct there by the accident of
matching convention, and wrong the moment a rebase moves one of them onto a tree that names the
parameter differently.

The patch now calls `ksu_handle_setuid(work->new_uid, work->old_uid)` - the same tuple this tree's own
bridge passes, and a declared function rather than the implicit declaration the old call was silently
relying on. `tools/check_rkp_branch.py --tree <tree>` is what keeps it honest: verified exit 0 on the
patched `v4.2.0-rc2`, KernelSU-Next `v3.4.0`, KernelSU `v3.3.0` and `v3.2.5` trees, and exit 1 with the
defect named on a `v4.2.0-rc2` checkout carrying the previous patch.

## Not verified

The fix above is read off the kernel log of the run that failed, not off a run that succeeded: no
module built from this patch has been loaded on the device yet. What is still open is the rest of the
question the Next delta needed a device to answer - what `su` can actually do on a Samsung kernel with
no dispatcher, and whether the kretprobe stand-ins carry the same work the tracepoint path does. The
manager fd is the first of those to have a concrete, checkable answer.

The manager side is not part of the patch:
a ReSukiSU manager is a third-party APK whose signature the kernel validates, with
`ksud kernel dynamic-manager set <size> <hash>` as the way a manager gets registered at runtime.
