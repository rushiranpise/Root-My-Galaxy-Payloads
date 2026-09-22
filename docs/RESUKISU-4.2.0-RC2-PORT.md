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

`tools/check_rkp_branch.py --tree <patched tree>` exits 0 with two notes it prints for the record:
the early-return branch makes 4 calls and registers none of the dispatcher's registrations (intended),
and `on_boot_completed()` calls `ksu_selinux_hide_drop_backup_if_unused()`, which the late-load branch
does not. The second is a cleanup rather than a feature arming - unlike the `avc_spoof` call whose
absence the same check found in KernelSU-Next - so it is left alone and stated here instead.

## Not verified

No compile and no device run yet. The patch applies cleanly to a fresh `v4.2.0-rc2` checkout and both
edited Rust files parse under the crate's edition; the type check is the CI build's job. Nothing here
has been loaded on a phone, and the question the Next delta needed a device to answer - what `su` can
actually do with no dispatcher - is open for this tree too. The manager side is not part of the patch:
a ReSukiSU manager is a third-party APK whose signature the kernel validates, with
`ksud kernel dynamic-manager set <size> <hash>` as the way a manager gets registered at runtime.
