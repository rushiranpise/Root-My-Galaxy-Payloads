# KernelSU-Next v3.4.0 upgrade

**Status: rebased, nothing compiled, nothing published, not device-tested.**

KernelSU-Next released `v3.4.0` on 2026-09-21 (13:36 UTC, tag `1a879d6a`, "manager: add custom animated
splash screen"). The Samsung patch has been re-derived against it by hand and the result verified against
a fresh clone of the tag, but **it has not been through the DDK**, so no module and no `ksud` exist for
it and nothing here has been near a phone. The patch is committed as `a268e06`:

```
kernelsu/patches/KernelSU-Next-v3.4.0-samsung-kdp-rkp-defex.patch
```

This is the first upgrade record for the **Next** leg. The flavour-independent background lives in
[`KERNELSU-3.3.0-UPGRADE.md`](KERNELSU-3.3.0-UPGRADE.md) and still applies: the DDK image, the libclang
and assembler requirements for `ksud`, the staged-daemon split and why it exists, and the `check_symbol`
bar a published pair has to clear. Only the Next-specific facts are restated here.

`tiann/KernelSU` is still **v3.3.0** (2026-08-28) and its latest release is unchanged, so this moves one
leg, not both.

## What was verified

| claim | how |
| --- | --- |
| the patch applies clean to `v3.4.0` | `git apply --check` = 0 on a full clone of tag `1a879d6a`, all 16 files |
| it is genuinely a v3.4.0 patch | the `v3.3.0` Next patch refuses on `kernel/Kbuild:62`, `kernel/Kconfig:35`, `kernel/core/init.c:25` |
| the delta is unchanged from `v3.3.0` | per-file added/removed lines are identical for 15 of 16 files; `kernel/Kbuild` differs only by the fallback hunk we removed |
| the Samsung logic did not drift | the four Samsung-authored files are byte-identical between the two patches |
| the tag gives the version | `30000 + 3294 = 33294`, and the release's own manager assets are named `…_33294-release.apk` |
| a three-way merge lands in three conflicts | `git -c merge.conflictStyle=diff3 apply -3`; one conflict in each of `kernel/Kbuild`, `kernel/Kconfig`, `kernel/core/init.c`, and the other 13 files clean |
| the watcher's rebase tool agrees | `tools/rebase_patch.py` reports the same three, resolves two, escalates `init.c` |
| the KernelSU2 rewrite is inert on this tag | running the workflow's `sed` over the four manifests leaves the tree unchanged |
| the DDK release did not move | upstream's `ddk-lkm.yml` still defaults to `20260828` at this tag |

**What that does not establish.** Nothing was compiled: no module for either KMI, no `ksud`, no DDK
container, no `cargo`. The symbol audit (`kernel/check_symbol` against a recovered `vmlinux`) was not run,
and neither was the relocation/CRC audit. The validation section below is owed in full before a pair is
published.

## What the rebase produced

1180 lines against the Next `v3.3.0` patch's 1187, over the same 16 files: twelve modified, four added whole
(`kernel/compat/samsung_kdp.c`, `kernel/compat/samsung_defex.c`, `kernel/compat/samsung_defex.h`,
`kernel/include/ksu_samsung_kdp.h`). Based on tag `v3.4.0` = `1a879d6a`; the Next `v3.3.0` patch was based
on `3b18216f`, whose 3214 commits are the 33214 the 3.3.0 record names.

## How much actually changed: nothing, and that is measured

The 3.3.0 record said the semantic delta was one extra parameter on two functions. This one is smaller
still, and it can be stated exactly: **the added and removed lines of our own delta are byte-identical
between the two patches for 15 of the 16 files.** Not "the Samsung sources did not drift" — the whole
delta, in every file, including the two userspace Rust files that make up the whole userspace half.

The one file that differs is `kernel/Kbuild`, and only by the two lines of the hunk that used to replace
`KSU_VERSION_FALLBACK := 1` with `KSU_VERSION_FALLBACK := 33000` — the hunk we removed on purpose, for the
reason in the version section below.

So the semantic content of this upgrade is **zero code**. What moved is where our hunks anchor, which is
the whole of the conflict section below: upstream rearranged three regions we insert into.

## Upstream churn in the files we touch

The range `v3.3.0..v3.4.0` is 80 commits, 151 files, +8747/-1943. Of the 16 files our patch touches,
upstream changed nine:

| file | upstream +/− | our delta (lines) |
| --- | --- | --- |
| `kernel/Kbuild` | +12 / −2 | 19 |
| `kernel/Kconfig` | +8 | 31 |
| `kernel/core/init.c` | +9 / −7 | 25 |
| `kernel/feature/sucompat.c` | +30 / −4 | 3 |
| `kernel/hook/arm64/patch_memory.c` | +37 / −2 | 7 |
| `kernel/hook/syscall_hook_manager.c` | +4 / −2 | 323 |
| `kernel/policy/app_profile.c` | +41 | 11 |
| `userspace/ksud/src/late_load.rs` | +1 | 22 |
| `userspace/ksud/src/utils.rs` | +43 / −2 | 54 |

and left seven untouched: the four Samsung-authored files, `kernel/hook/arm64/syscall_hook.c`,
`kernel/hook/syscall_hook.h`, `kernel/hook/tp_marker.c`.

The number worth reading is `syscall_hook_manager.c`: our largest hunk in the whole patch, 323 lines, and
upstream moved four lines in it. That is the file to read first if a build fails, and it is why a rebase
this large in upstream's tree can still be this cheap for us.

## Why it is not just a version bump

| upstream change | why it reaches us |
| --- | --- |
| `kernel: Support out-of-tree builds against generic Linux` (`8b9d7a7a`) | it rewrote the `KSU_KERNEL_DIR` detection (`ifeq ($(filter /%,$(src)),)` → `ifneq ($(KBUILD_EXTMOD),)`) in the same block our selinux include sits in — one of the three conflicts |
| `fix(kernel): Miscellaneous build errors in syscall_hook_manager and arm64 patch_memory` (#3738, `a951768d`) | the two files our delta lives in. The fix is tiny — a `ksu_flush_icache` macro definition that lacked its arguments, and `linux/sched/task_stack.h` moved out of a version guard — but `patch_memory.c` is where our no-patch-text guard lives |
| `feat: new version matching detection mechanism` (#3516, `e801e16b`) | **the one that changes what a mismatch means.** `KERNEL_SU_UAPI_VERSION` goes 3 → 4, with a new `KSU_GET_INFO_FLAG_BUNDLED` and a `ksu_bundled` module param. The manager now warns **only** on a UAPI mismatch, and labels an external LKM *Custom* instead of nagging about its version |
| `ksud: Add boot image LKM injection` (#1446, `86e8e246`) + two follow-ups | 4,247 new lines of userspace in this range: `lkm_image.rs` (3,172), `lkm_image_btf.rs` (680), `lkm_image_bootstrap.S` (191), `risk.rs` (204). Upstream is growing its own loader over the same ground our `.ksud-stage` handoff covers |
| `build(deps): migrate dependencies to new repos` (#3723, `9c1911c1`) and `build(dep): ksuinit: migrate dependency to new repo` (`a9f76ce7`) | **already in this tag.** `github.com/Kernel-SU/` no longer appears anywhere in the tree — the git dependencies left are `KernelSU2` (`java-properties`, `ksu_props`, `rustix`), `5ec1cff/android_bootimg` and `kstep/kernlog.rs` — so the rewrite the workflow applies for the 3.3.0 leg is a no-op here, verified by running it and finding the tree unchanged |
| `kernel: retain capabilities across execve for non-root profiles` (#3710), `fix(kernel, uapi, ksud): make fd wrapper work with app profile` (#3679), `kernel: selinux: Bypass dynamic wrappers conditionally` (`1b2316be`), `feat(ksud): Implement SIGSYS handler` (#3677) | the credential, fd-wrapper and SELinux corners the Samsung KDP/DEFEX delta is built around |
| `fix(kernel): Reject all signature block id except v2` (#3700, `a161d0bc`) | module and manager APK are a pair because the module verifies the manager's signing block |

## The three conflicts, and how each was resolved

Both userspace files that conflicted in the 3.3.0 rebase — `late_load.rs` and `utils.rs`, the second of
which was the only *real* conflict that time — now apply **cleanly**, and so does
`kernel/hook/arm64/patch_memory.c`, which was a placement conflict last time. The `install` /
`finish_install` split therefore carried over verbatim, signature and all.

### 1. `kernel/Kbuild` (1 conflict) — textual collision

Upstream's new `ifeq ($(CONFIG_KSU_X86_PATCH_SYSCALL_DISPATCHER),y)` block now occupies the `ccflags`
region our four `CONFIG_KSU_SAMSUNG_*` blocks go in. The ancestor region is empty: both sides only
**add** lines.

**Resolution:** upstream's x86 block first, then our four Samsung blocks and the selinux include. The
`-I$(KSU_KERNEL_DIR)/..` addition stays on the `else` branch's `ccflags` line, which survived upstream's
`ifneq ($(KBUILD_EXTMOD),)` rewrite. No semantic conflict.

### 2. `kernel/Kconfig` (1 conflict) — textual collision

The same shape one file over: our four entries go at the end of the entry list, and upstream's
`KSU_X86_PATCH_SYSCALL_DISPATCHER` is now what is at the end of it.

**Resolution:** our four entries after upstream's, unchanged. Also add/add against an empty ancestor — no
semantic conflict.

### 3. `kernel/core/init.c` (1 conflict) — **the one that needs a decision**

```diff
<<<<<<< ours
#if defined(__x86_64__) && !defined(CONFIG_KSU_X86_PATCH_SYSCALL_DISPATCHER)
    // If the kernel has the hardening patch, X86_FEATURE_INDIRECT_SAFE must be set
||||||| base
#if defined(__x86_64__)
    // If the kernel has the hardening patch, X86_FEATURE_INDIRECT_SAFE must be set 
=======
	int ret;
#if defined(__x86_64__)
    // If the kernel has the hardening patch, X86_FEATURE_INDIRECT_SAFE must be set 
>>>>>>> theirs
```

This is a different shape from the other two. Upstream **narrowed** the guard and stripped a trailing
space from the comment under it; our patch inserts `int ret;` immediately before that guard. The ancestor
holds the line, so one side rewrote what the other side edits: the two are alternatives, not two
additions.

**Resolution:** upstream's narrowed guard wins, trailing whitespace and all, and our `int ret;` goes above
it — inside `kernelsu_init()`, after the `#ifdef MODULE` bundled param. The KDP/DEFEX ordering below it is
untouched and unchanged from 3.3.0: symbol resolver, then `ksu_samsung_kdp_init()`, then
`prepare_creds()`, then `ksu_samsung_defex_init()`, then `ksu_syscall_hook_init()`, with the KDP teardown
on each failure path and `ksu_samsung_defex_exit()` / `ksu_put_cred()` / `ksu_samsung_kdp_exit()` on the
way out.

That decision is the reason this release still needed a human even though the other two conflicts are
insertions: the rebase tool resolves add/add against an empty ancestor and refuses anything that changes a
line the ancestor had, which is exactly right here.

## A feature the dispatcher check was hiding

Our delta makes `ksu_syscall_table_hook` report a failed write, and `ksu_syscall_hook_manager_init`
returns early when the dispatcher could not be installed. On a Samsung target RKP refuses the write to
`sys_call_table` — the dm1q record's fourth postmortem has the abort that proves it — so that early
return is the branch these devices take. Upstream's init ends with three registrations:

```c
ksu_setuid_hook_init();
ksu_sucompat_init();
ksu_avc_spoof_init();
```

The first two are exactly what the RKP branch replaces with its own kretprobe/kprobe pair. The third was
not named in that branch at all. So on every target whose syscall table cannot be patched the
`avc_spoof` feature handler (id 10003) was never registered, `ksud feature check avc_spoof` answered
`unsupported`, and the manager's **AVC spoofing** switch read as unavailable — on a device where the
only thing missing was a registration.

What the feature needs is not the dispatcher: it reaches `slow_avc_audit` through a kprobe of its own,
registered from `ksu_avc_spoof_late_init()` at boot-completed, and it never touches a syscall. Both Next
patches now call `ksu_avc_spoof_init()` in that early return as well, with `ksu_avc_spoof_exit()` in the
matching one, in the order upstream uses. Five lines per patch: a regenerated patch differs from the one
before it only in `syscall_hook_manager.c`'s two hunks and their counts.

What that changes, and what it does not: the switch becomes real, because the handler now exists and can
be read and set. Nothing about *when* the kprobe goes in changes either way — and that turns out to be a
second, independent gap, one this delta walks into on every device it roots.

## The event a late-loaded module never gets

`dispatch.c` skips the boot-completed event when the module was loaded after boot, which is the only way
this delta reaches a locked device, so `on_boot_completed()` never runs for one. It is where the spoof is
armed:

```c
ksu_boot_completed = true;
track_throne(true);
ksu_selinux_hide_drop_backup_if_unused();
ksu_avc_spoof_late_init();      // get_sid(), then the slow_avc_audit kprobe
```

`ksu_avc_spoof_late_init()` is what sets its own `boot_completed` flag, resolves the two SIDs and
registers the kprobe; until it runs, `disable_spoof` keeps the hook returning immediately. The handler is
registered and `ksud feature get avc_spoof` reports the compile-time default — `ksu_avc_spoof_enabled` is
a `static bool = true` in `extras.c` — so the switch reads **enabled** while the feature does nothing. The
registration fix alone therefore makes the switch real and still leaves it inert on the devices it was
written for.

The two states look identical in the manager, and one command separates them: toggle **AVC spoofing** off
and back on, then read the log. A driver whose `boot_completed` is set prints `avc_spoof/get_sid: su_sid:
…` and `slow_avc_audit spoofing enabled!`; one that only took the flag prints `avc_spoof: set to 1` and
nothing else. `ksud feature get avc_spoof` cannot tell them apart — what it reports is that flag, not the
kprobe.

The late-load branch of `kernel/core/init.c` arms it, beside the `ksu_boot_completed = true` it already
sets. The guard covers this as well: it derives the requirement from the tree's own `on_boot_completed()`
instead of asserting a call, so a tree whose event arms nothing — the tiann leg, whose feature set has no
`avc_spoof` at all — is not failed for it, and it reports the rest of the difference without failing on
it. On the Next trees that report is one name, `ksu_selinux_hide_drop_backup_if_unused()`, which
upstream's own late path skips too: upstream's call, not a defect this delta introduced.

The tiann leg is untouched, and by evidence rather than by assumption: its 3.3.0
`ksu_syscall_hook_manager_init` ends at `ksu_setuid_hook_init()` and `ksu_sucompat_init()`, with no
feature registration behind them, so our early return skips nothing there.

One consequence of where the fix lives: it reaches a device only through a rebuilt pair. The published
artifacts are what the loader gets, so this changes nothing on a phone until the pair is republished.

A check now runs wherever the patch is applied — `tools/check_rkp_branch.py --tree KernelSU`, in all four
jobs of the build workflow. It reads both shortcuts out of the patched files: the early-return branch in
`syscall_hook_manager.c`, which may not be missing an `_init` the rest of the function calls (the two
Samsung stand-ins are the only allowed substitutions), and the late-load branch in `init.c`, which must
do whatever that tree's own `on_boot_completed()` does about the spoof. `--self-test` proves both can
fail without failing a correct tree. Run against the previous patch the first check names exactly
`ksu_avc_spoof_init()`; run against a tree patched before this section it names
`ksu_avc_spoof_late_init()`. Upstream adds to that tail, and the next addition would otherwise be dropped
the same way — by someone reading a diff rather than by something refusing one.

A tree check only covers what the run applied, and a run applies one flavour at one ref — the default is
`kernelsu-next` at `v3.3.0`. So the same guard is also made against the diffs themselves: the `ksud` job
runs `--self-test` and then `tools/check_rkp_branch.py --patches kernelsu/patches`, which needs no
checkout and no network, and refuses any published patch that names the spoof but does not leave the
late-load branch of `init.c` calling it. That is what makes the 3.4.0 patch — which nothing builds by
default — as guarded as the one a run does apply. The two are run together on purpose: a guard whose
fixtures have rotted passes exactly like a guard with nothing to catch, so the self-test is what says the
check below it can still fail.

## The version number is derived from the tag, not written down

`kernel/Kbuild` computes the module's version as `30000 + git rev-list --count HEAD` and prints it while
configuring. At `v3.4.0` that is **33294** (3294 commits), and the release's own assets confirm the
arithmetic is the same one upstream numbers its manager with:

```
KernelSU_Next_v3.4.0_33294-release.apk
KernelSU_Next_v3.4.0-spoofed_33294-release.apk
```

The same rule on `v3.3.0` gives `30000 + 3214 = 33214`, which is the number that release's manager
carries and the number CI logged for the module it built. Nothing in this repository writes a version
down. The `KSU_VERSION_FALLBACK` hunk that used to hand-set it is gone from this patch — it was dead code
in every build this workflow performs, since the DDK image has `git`, and wrong by 214 if it had ever
run. What replaces it is `tools/check_kernel_version.py`, which asserts each build's module version
against the commit count of the tree it built and fails on the fallback branch (which reports 1), on a
shallow clone (which counts 1 and reports 30001), and on a mismatch. Its limit: it asserts the version,
not `KSU_VERSION_TAG`, whose own fallback branch is `v0.0.1`.

The `v3.3.0` patch keeps its line. That file is the record of what its pair was built from, and an unread
`:=` changes nothing.

### The other half of that number

The daemon derives its own version the same way, one file over: `userspace/ksud/build.rs` reads
`git rev-list --count HEAD` of the checkout it compiles in and writes `30000 + count` as `VERSION_CODE`,
with `VERSION_NAME` from `git describe --tags --always`. On one commit the two halves therefore agree by
construction — which is exactly why nothing compared them. They are built by two different jobs, from two
different clones, and the job that publishes them has neither, so a pair could carry one number in its
module and another in its daemon, and nothing would say so:

- `ksu_ref` may be a branch, resolved twice minutes apart, so the module is built at one commit and the
daemon stamps another;
- a commit past the tag leaves `Kbuild` naming the nearest tag (`v3.4.0`) while `build.rs` names
`3.4.0-4-g1a879d6a`, and the feed is named from the ref — three names for one build;
- a shallow or tagless checkout takes `Kbuild`'s fallback of 1, or `build.rs`'s of `(0, "0.0.0")`, and the
app reads the second as *no KernelSU at all*.

`tools/check_pair_version.py` refuses each of those, and the pair is compared three times over: the
module's receipt (written beside the module by `check_kernel_version.py`, and travelling in the same
artifact), the `VERSION_CODE`/`VERSION_NAME` the build script wrote into the daemon's `OUT_DIR`, and the
name the daemon binary actually carries. The feed entry is then named from the tag the check verified
rather than from the input, so the payload id's version suffix and the display name are the same fact the
two artifacts were stamped with. The receipts are removed before anything is staged: they are evidence,
not artifacts, and nothing reads them at runtime.

At publish time the checkouts are gone and the check runs again on the two receipts, which is weaker by
construction — an exact tag cannot be re-established from a receipt — and the tool prints that it is
doing exactly that. Its self-test is the decision table, so each of the three failures above is a case in
it (`--self-test`, 12 cases).

## Building it

The recipe in [`../kernelsu/README.md`](../kernelsu/README.md) applies per KMI, with the 3.3.0 record's
traps (DDK image, exact target release, assembler and libclang for `ksud`, `fetch-depth: 0` and
`fetch-tags: true`). What this tag changes:

1. **The KernelSU2 rewrite is a no-op here.** The workflow still runs the `sed`; on this tag it matches
   nothing, because the manifests already point at `KernelSU2`. The 3.3.0 leg is the one that needs it.
2. **No fallback number to set.** `tools/check_kernel_version.py` runs in the module job and compares the
   build's own output against the checkout it built — a full-history checkout, or the job fails.
3. **DDK release unchanged.** `ddk_release=20260828`, as upstream's own `ddk-lkm.yml` still defaults to.
4. **The `ksud` job builds more than it did**, because of the LKM-injection work in this range.
5. **The 5.15 family needs the ucount guard, and the patch carries it.** `kernel/compat/samsung_kdp.c`
   declares two function-pointer typedefs whose second parameter is the ucounts enum. Samsung's
   `android13-5.15` still names that type `enum ucount_type` — only 5.16+ renamed it `enum rlimit_type` —
   so the unguarded name is an implicitly declared, incomplete type there and clang refuses the file with
   `-Wvisibility`. The patch now picks the name by `LINUX_VERSION_CODE`, exactly as
   `KernelSU-v3.3.0-samsung-kdp-rkp-defex.patch` already does, which is what lets the four
   `android13-5.15` targets build this flavour at all.

For a publishable pair for the S25U target, which is what the feed serves:

```sh
gh workflow run ksu-build.yml \
  -f flavor=kernelsu-next \
  -f ksu_ref=v3.4.0 \
  -f ddk_release=20260828 \
  -f kmi='["android15-6.6","android14-6.1"]' \
  -f target_id=pa3q-S938USQSCCZF9 \
  -f target_release=6.6.98 \
  -f publish=true
```

The patch has to be in place for `PATCH` to resolve, and it is. Because the patch is already committed,
the upstream watcher will not consider v3.4.0 a rebuild — the dispatch above is the way it goes live.

## Validation before any of it is published

The bar is the same one the 3.2.5 pairs met, per profile: late-load ends in `u:r:ksu:s0`; `su` is granted
and survives enforcing; `ksud --help` still lists `late-load`, and lists `soft-reboot` if the app is to
offer it; modules reload on the userspace restart; and the app's full chain runs end to end — exploit,
late-load, `su`, restart.

Three readings specific to this release:

- **Install the matching manager**: `KernelSU_Next_v3.4.0_33294-release.apk` from the `v3.4.0` release,
  or the `-spoofed_33294` build if the spoofed package is wanted. Keep the 3.3.0 manager APK: going back
  to the tested pair means going back to both halves.
- **Expect *Custom* in the manager's version field.** #3516 makes the manager warn only on a UAPI
  mismatch and label any external LKM as custom. Ours is external and late-loaded, so *Custom* is the
  correct reading and not a fault. A warning that is *not* about UAPI, or a module that fails the UAPI
  handshake during late-load, is the thing to chase.
- **`syscall_hook_manager.c` is the file to watch**, because our largest hunk lives in it and upstream
  touched it in this range — not much, but in a fix for its build errors.

The 6.6 profile (`galaxy-s25-series-*`, the S25U target) first, as before: it is what the app is
developed against, and its failure mode is the one the whole Samsung delta exists for.

## What the watcher does with a release like this

`tools/rebase_patch.py` was run against this exact release as a check of the tool, not as the way the
patch was made, and it reported:

```
rebase: mode=conflict
  kernel/Kbuild: 1 conflict(s), each side only adds lines
  kernel/Kconfig: 1 conflict(s), each side only adds lines
  kernel/core/init.c: 1 touching an ancestor line
```

So for a release shaped like this one, the automated path resolves the two insertion conflicts itself and
stops with the `init.c` diff in the report — one hunk to read, not a rebase to do. That is the case that
should commit itself, and the case that should not. `init.c` needed the decision above, and the tool was
right to refuse it.

## The app side is nothing

Unlike the tiann leg, nothing in the app moves for this release. The manager-version picker reads the
published releases for the chosen flavour, so v3.4.0 appears in it on its own, and the spoofed build is
matched by label because that build renames its package per release.

What changes is the feed. A published Next pair for this target becomes an entry selected by daemon file
name, which carries no version:

| | |
| --- | --- |
| payload id | `pa3q-S938USQSCCZF9-ksun340` |
| display name | `Galaxy S25 Ultra \| KernelSU-Next 3.4.0 (test)` |
| daemon | `kernelsu/ksud-next-pa3q-S938USQSCCZF9-kdp` |
| flavour | `kernelsu-next` |

The daemon's file name is unchanged from the 3.3.0 pair, so the publish job rewrites that entry in place
rather than adding a sibling beside it. Keeping both side by side is possible — the ids differ by suffix —
but then the sheet offers two Next entries for one device and the 3.3.0 one is the tested one.

## Deliberate non-goals

- **Not rebasing onto `main`.** A tag is what the patch keys off and what a published artifact is
  reproducible from.
- **Not adopting upstream's LKM-injection path in place of our staged handoff.** `ksud boot-patch-v2`
  takes a boot image and an output path, decompresses the kernel, recovers kallsyms and BTF from the raw
  Image, links a bootstrap into a kernel text cave, rewrites the `bl` call site in `kernel_init()` that
  reaches it, appends a capsule holding the module with its fixups already resolved, and repacks the
  image. The only thing it writes is that image (the single `write_all` in its 3,172 lines; it never
  mentions `/data/adb`, a daemon, or an install), so it does not do what the handoff does — it removes
  the situation the handoff exists for, at the price of flashing a boot image, which needs an unlocked
  bootloader. Our devices are locked; this app's own *Protect image partitions* says as much, that
  keeping the image partitions read-only "blocks flashing images from the phone and KernelSU installs
  that patch boot." If upstream ever ships the same injection without a flash in front of it — entering
  the patched image some other way — that becomes worth a fresh look, because text rewritten into the
  image is never written at runtime, which is what Samsung's EL2 protection refuses us.
- **Not keeping the `KSU_VERSION_FALLBACK` hunk.** The number belongs to the tag.
- **Not publishing anything.** See the status line at the top.
- **Not touching the tiann leg.** It is still `v3.3.0` and its record is unchanged.
