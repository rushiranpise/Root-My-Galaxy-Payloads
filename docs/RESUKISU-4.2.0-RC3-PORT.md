# ReSukiSU `v4.2.0-rc3`: carrying the Samsung delta one release candidate forward

**Status: rebased by hand, applies clean to the tag, nothing compiled, nothing published, not
device-tested.**

ReSukiSU tagged `v4.2.0-rc3` on 2026-09-22 (`239e1e88`, "manager: refine install page and other minimal
changes (#417)"), nine days after the `v4.2.0-rc2` this flavour was ported onto. The delta in
[`RESUKISU-4.2.0-RC2-PORT.md`](RESUKISU-4.2.0-RC2-PORT.md) is carried forward unchanged except where the
two trees disagree, and the result is committed as:

```
kernelsu/patches/ReSukiSU-v4.2.0-rc3-samsung-kdp-rkp-defex.patch
```

This is a rebase, not a port: the same delta onto a newer tag, where rc2 was the delta being written for
a tree we had never carried. The `rc2` record is the background for *what* the delta does; this one is
about what moved underneath it. The upstream watcher reached the same conclusion on its own - run
`35937188894` opened [issue #16](https://github.com/rushiranpise/Root-My-Galaxy-Payloads/issues/16)
rather than publishing, because the two conflicted files are ones where a side changes a line the
ancestor had, and that is a decision rather than a merge.

## What moved

The range `v4.2.0-rc2..v4.2.0-rc3` is 27 commits, 95 files, +1883/−1223. Of the 17 files our patch
touches, upstream changed six - and four of those six are one-line edits in files where our hunk is
nowhere near them:

| file | upstream +/− | our delta (lines) |
| --- | --- | --- |
| `kernel/Kbuild` | +4 | 18 |
| `kernel/core/init.c` | +1 / −3 | 26 |
| `kernel/hook/arm64/patch_memory.c` | +1 / −1 | 12 |
| `kernel/hook/syscall_hook_manager.c` | +3 / −1 | 364 |
| `userspace/ksud/src/android/late_load/mod.rs` | +1 / −1 | 11 |
| `userspace/ksud/src/android/utils.rs` | +28 / −2 | 65 |

and left eleven untouched: the four Samsung-authored files (`samsung_kdp.c`, `samsung_defex.c`,
`samsung_defex.h`, `ksu_samsung_kdp.h`), `kernel/Kconfig`, `kernel/feature/selinux_hide.c`,
`kernel/hook/arm64/syscall_hook.c`, `kernel/hook/syscall_hook.h`, `kernel/hook/tp_marker.c`,
`kernel/policy/app_profile.c`, `kernel/selinux/selinux.c`.

What the four kernel edits are, and why none of them reaches us:

| upstream change | why it does not reach the delta |
| --- | --- |
| `Kbuild`: a `KERNEL_TYPE := Non-GKI (Legacy)` line for kernels below `VERSION 4` | the `KERNEL_TYPE` block is at line ~112; our hunks are the `kernelsu-objs` list at line ~18 and the Samsung `ccflags` block after the selinux include. Different region, no overlap |
| `init.c`: `setup_ksu_cred()` drops the `#ifdef KSU_COMPAT_REQUIRE_SESSION_KEYRING` guard | that function is above `kernelsu_init()`; our hunks are the KDP/DEFEX ordering inside `kernelsu_init()` and the reverse unwind in `kernelsu_exit()` |
| `init.c`: the boot-completed path calls `track_throne(TRACK_THRONE_FORCE_SYNCHRONOUS)` without `TRACK_THRONE_FORCE_SEARCH_MGR` | the delta does not call `track_throne` at all; it merges cleanly and the flag is upstream's own decision |
| `patch_memory.c`: `KSU_HAS_NEW_DCACHE_FLUSH` → `KSU_FLUSH_DCACHE_AREA_NOT_FOUND` in the `ksu_flush_dcache` guard | our delta adds the `CONFIG_KSU_SAMSUNG_NO_PATCH_TEXT` refusal elsewhere in the file, not this guard |
| `syscall_hook_manager.c`: `<linux/sched/task_stack.h>` moves under `#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 11, 0)` | our delta is the Samsung kretprobe stand-ins further down; the include block is untouched by us |

So the kernel half of the delta is byte-identical between the two patches, and the two files that
conflicted are both in the userspace half - the same two the watcher escalated.

## The two conflicts, and how each was resolved

Both conflicts are in `userspace/ksud/src/android/`, and both are the same shape: upstream and our patch
each rewrite the *same* line, so the ancestor holds the answer and a three-way merge refuses to guess.
They are not additions to be interleaved.

### 1. `late_load/mod.rs` (1 conflict) — a call site

Upstream added a second argument to `install`; our patch changed the call to `finish_install` because the
staging already happened above it. One line, two rewrites:

```diff
<<<<<<< ours
    utils::install(None, None).context("Failed to install ksud")?;
||||||| base
    utils::install(None).context("Failed to install ksud")?;
=======
    utils::finish_install(None).context("Failed to finish ksud installation")?;
>>>>>>> theirs
```

**Resolution:** upstream's parameter count wins, our function name wins -
`utils::finish_install(None, None)`. The `data_path` is `None` here because late-load has no backup
directory to restore; the two `None`s are the two arguments upstream's `install` now takes. The staging
call above it (`stage_daemon_from("/data/local/tmp/.ksud-stage")`) is unchanged, and this line is still
the *finish* half of the split - which is the whole point of the path: on a late-load the process doing
the install has no KernelSU policy until the module arrives, so the copy has to be made before the load
and the context-setting steps have to run after it.

### 2. `utils.rs` (2 conflicts) — the split itself

This is where the delta lives, and upstream rewrote both halves of it: the signature, and the body that
copies the running binary into place.

```diff
<<<<<<< ours
pub fn install(libadbroot: Option<PathBuf>, data_path: Option<PathBuf>) -> Result<()> {
||||||| base
pub fn install(libadbroot: Option<PathBuf>) -> Result<()> {
=======
pub fn stage_daemon() -> Result<()> {
...
pub fn stage_daemon_from(staged_exe: impl AsRef<Path>) -> Result<()> {
>>>>>>> theirs
```

```diff
<<<<<<< ours
    let _ = std::fs::remove_file(defs::DAEMON_PATH);
    std::fs::copy(
        // We should use /proc/self/exe, DO NOT resolve the real path
        // So that if someone execute /data/adb/ksud install, ksud won't be removed unexpectedly
        "/proc/self/exe",
        defs::DAEMON_PATH,
    )?;
||||||| base
    let _ = std::fs::remove_file(defs::DAEMON_PATH);
    std::fs::copy(
        std::env::current_exe().with_context(|| "Failed to get self exe path")?,
        defs::DAEMON_PATH,
    )?;
=======
    std::fs::rename(staged_exe.as_ref(), defs::DAEMON_PATH)...;
    chown(defs::DAEMON_PATH, Some(Uid::ROOT), Some(Gid::ROOT))?;
    #[cfg(unix)]
    set_permissions(defs::DAEMON_PATH, Permissions::from_mode(0o755))?;
    Ok(())
}

pub fn finish_install(libadbroot: Option<PathBuf>) -> Result<()> {
>>>>>>> theirs
```

Upstream's two changes are independent of each other and both have to be honoured; our patch's single
change recuts the function into three. The resolution keeps the split and threads upstream's new
argument through it:

- **`finish_install(libadbroot, data_path)`** - the new parameter is added, and the body upstream grew
  for it (the boot-backup move out of `data_path.join(KSU_TEMP_BACKUP_DIR_NAME)`) is already in the
  merged text and uses it. Nothing else in that function changed.
- **`install(libadbroot, data_path)`** stays a two-line wrapper - `stage_daemon()?;
  finish_install(libadbroot, data_path)` - so upstream's own caller in `cli.rs`
  (`utils::install(libadbroot, data_path)`) keeps working without an edit to a file we do not touch.
- **`stage_daemon()` adopts upstream's `/proc/self/exe`.** Our version read
  `std::env::current_exe()`, which resolves symlinks; upstream's comment says exactly why that is wrong -
  if the process *is* `/data/adb/ksud`, resolving the path is how the old code came to delete the daemon
  it was running from. A read of `/proc/self/exe` always gets the running inode. Our `stage_daemon` kept
  the `current_exe == DAEMON_PATH` early return, which is now the thing that makes the two agree: when
  the process is the installed daemon there is nothing to stage, and writing over a running executable
  would fail with `ETXTBSY` anyway.
- **`stage_daemon_from()` is untouched** - it renames a copy that some other process staged, which is the
  late-load path and has no counterpart upstream.

The result is the same three functions the rc2 patch introduced, in the same order, with the two
signatures upstream widened and the one read upstream corrected.

## What the rebase produced

| | `rc2` patch | `rc3` patch |
| --- | --- | --- |
| files | 17 | 17 |
| added / removed | +1198 / −43 | +1200 / −44 |
| lines | 1632 | 1636 |

The two added lines are the `data_path` parameters (one on `finish_install`, one on `install`); the one
removed is the `&current_exe` read that became `/proc/self/exe`. Every other line in the patch - all
fifteen kernel files - is the same text in the same order, verified by diffing the two patches' added
lines file by file: the only differences are in the last two hunks, and the only structural difference
is a `}` that moved from the end of `install`'s body to the end of `stage_daemon_from` when the merge
folded the two conflicts together.

## What was verified

| claim | how |
| --- | --- |
| the patch applies clean to `v4.2.0-rc3` | `git apply --check` = 0 on a full clone of tag `239e1e88`, all 17 files |
| it is genuinely an rc3 patch | the `rc2` patch does not apply strictly to `rc3`; a three-way merge conflicts in exactly the two files below |
| the delta did not drift in the kernel half | the added lines of the two patches are identical for all 15 kernel files |
| the watcher's rebase tool agrees with the manual rebase | `tools/rebase_patch.py` reports `mode=conflict` on the same two files, `1` and `2` conflicts, both "touching an ancestor line" |
| upstream's new call convention is satisfied | `userspace/ksud/src/android/cli.rs` still calls `utils::install(libadbroot, data_path)`, and the resolved `install` takes both |
| the version name is accepted by the pair check | `check_pair_version.py`'s `PRERELEASE` pattern covers `-rc3` as it does `-rc2`; no tool change |
| the Samsung shortcuts did not drift | `tools/check_rkp_branch.py --tree <tree>` exits 0 on the patched rc3 tree: the early-return branch registers none of the normal path's 4 registrations (intended), the late-load branch skips only `ksu_selinux_hide_drop_backup_if_unused()`, and `ksu_handle_setuid()` is read as taking the uid moved *to* as argument 0 - the convention the delta's kretprobe passes |
| the tools still hold | `pairs.py --self-test` 19/19, `check_pair_version.py --self-test` 14/14 |

One build fix lives in the patch itself, for the 5.15 family: `kernel/compat/samsung_kdp.c` declares two
function-pointer typedefs whose second parameter is the ucounts enum, which Samsung's `android13-5.15`
still names `enum ucount_type` and only 5.16+ renamed `enum rlimit_type`. Declared unguarded, that name is
an implicitly declared, incomplete type on 5.15 and clang fails the compile with `-Wvisibility`; the patch
now selects it by `LINUX_VERSION_CODE`, matching `KernelSU-v3.3.0-samsung-kdp-rkp-defex.patch`.

**What that does not establish.** Nothing was compiled: no module, no `ksud`, no DDK container, no
`cargo`, and no device. The symbol audit (`kernel/check_symbol` against a recovered `vmlinux`) and the
relocation/CRC audit were not run against this tree. The validation bar in the rc2 record - late-load
ending in `u:r:ksu:s0`, `su` surviving enforcing, the `ksu_handle_setuid(work->new_uid, work->old_uid)`
hand-off being observed to crown a manager - is owed in full before a pair built from this patch is
called tested.

## Publishing

The patch existing is what unblocks the rebuild: the watcher's `detect` job sees the file, treats rc3 as
already-patched, and does nothing on its own. A forced, scoped dispatch is the way the pair is rebuilt:

```sh
gh workflow run upstream-watch.yml -f flavor=resukisu -f force=true -f targets=pa3q-S938USQSCCZF9
```

which resolves `refs[resukisu]=v4.2.0-rc3`, plans only the `pa3q` pair, and calls `ksu-build.yml` to
build it. The matrix builds and publishes nothing; the run's own `publish` job collects what it built
and commits the batch once, so a rebuild of many pairs is one commit rather than one push per pair. What
lands is the same feed entry, rewritten in place by daemon file name:

| | |
| --- | --- |
| payload id | `pa3q-S938USQSCCZF9-rsksu420` |
| display name | `Galaxy S25 Ultra \| ReSukiSU 4.2.0-rc3 (test)` |
| daemon | `kernelsu/ksud-rsksu-pa3q-S938USQSCCZF9-kdp` |
| flavour | `resukisu` |

The daemon file name carries no version, so the publish rewrites the `rc2` entry rather than adding a
sibling. Nothing in the app moves for this release: the flavour is chosen by the payload's own `flavor`
field and the sheet reads the version out of the entry, so a rebuilt pair is the whole of the change.

### The label the first publish left behind

The publish step rewrote the entry's `version` to `4.2.0-rc3` and left its `displayName`
reading `ReSukiSU 4.2.0-rc2` - the two facts the Next record insists are one fact, disagreeing in the
feed. The cause is a pre-release and the id arithmetic: `update_feed.py` retitled the label only when
the payload id's numeric suffix moved, and `4.2.0-rc2` and `4.2.0-rc3` both derive `420`, so the id
correctly stayed and the label silently did not follow. The tool now retitles the label from the version
string on its own, replacing the version it spells out whole so the old `-rc2` is consumed rather than
left trailing a new `-rc3`, and the rc2 → rc3 case is in its self-test. The entry reads `4.2.0-rc3` in
both places now. This is the second time a ReSukiSU release has taught the feed tooling about
pre-releases; the first was `check_pair_version.py` learning that `4.2.0-rc2` is a version name at all.

## Deliberately not ported or changed

- **Upstream's single-copy `install`.** Merging the two functions back together would undo the reason the
  split exists: on a late load the daemon has to be on disk before the module is, and the merge's own
  resolved text keeps `stage_daemon_from` callable from `late_load`.
- **Upstream's `KERNEL_TYPE := Non-GKI (Legacy)` line** - inert for every target we build (all arm64
  GKI 2.0), and not part of the Samsung delta.
- **The `rc2` patch.** It stays as it is: it is the record of what the published `rc2` pair was built
  from, and the `rc3` file is the delta for the newer tag.
- **Anything at all in the kernel half of the delta**, apart from where its hunks anchor - upstream did
  not move a line our kernel hunks touch in this range.
