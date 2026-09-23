/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Where `enforcing` sits inside `struct selinux_state` - and whether the bytes we can see agree
 * that it is there.
 *
 * This file is this project's own work; no third-party source was copied into it. What it
 * describes is the kernel's own struct, security/selinux/include/security.h:
 *
 *     struct selinux_state {
 *     #ifdef CONFIG_SECURITY_SELINUX_DEVELOP
 *             bool enforcing;
 *     #endif
 *             bool initialized;
 *             bool policycap[__POLICYDB_CAP_MAX];
 *             struct page *status_page;
 *             struct mutex status_lock;
 *             struct selinux_policy __rcu *policy;
 *             struct mutex policy_mutex;
 *     } __randomize_layout;
 *
 * Two of those lines decide everything here.
 *
 * `enforcing` exists only when CONFIG_SECURITY_SELINUX_DEVELOP is on. With it off the first byte of
 * the struct is `initialized`, there is no runtime switch to write, and a caller that assumes the
 * first byte is `enforcing` clears `initialized` instead - which tells the kernel SELinux was never
 * initialised. So the offset is never assumed: it is derived (see tools/btf_selinux_layout.py and
 * the `offset` module parameter), and the bytes around it have to prove it before a write happens.
 *
 * The struct is `__randomize_layout`, so under RANDSTRUCT the field order is shuffled and no fixed
 * offset means anything. Nothing announces RANDSTRUCT to a module at runtime either, which is why
 * the rule below is a shape test rather than an arithmetic one: only write where `enforcing` and
 * `initialized` are followed by the boolean `policycap` array, and refuse everywhere else.
 *
 * The check lives in this header, with no kernel dependency, so the kernel build and the host test
 * compile the same rule rather than two copies of it (tests/layout_test.c).
 */

#ifndef RMG_SELINUX_LAYOUT_H
#define RMG_SELINUX_LAYOUT_H

#ifdef __KERNEL__
#include <linux/types.h>
#else
#include <stdbool.h>
#include <stdint.h>
typedef uint8_t u8;
#endif

/* How many bytes from the symbol the shape test reads. The struct is far larger than this; the
 * window is deliberately small because it only has to reach past `policycap[0]`.
 */
#define RMG_LAYOUT_WINDOW 32

/* `policycap[]` entries that must read as booleans after `initialized`. __POLICYDB_CAP_MAX is
 * larger than this in every 6.6 kernel (the enum ends at POLICYDB_CAP_IOCTL_SKIP_CLOEXEC), so the
 * test never runs past the array into `status_page`, whose bytes would fail it anyway.
 */
#define RMG_LAYOUT_TAIL_BOOLS 8

/* A `bool` in the kernel is a byte holding 0 or 1. Anything else is a different field - most
 * likely a pointer's byte, which is what a shuffled layout or a wrong offset puts here.
 */
static inline bool rmg_layout_boolish(u8 b)
{
	return b == 0 || b == 1;
}

/*
 * True when `p[offset]` really does look like `selinux_state.enforcing` on a running system.
 *
 * The three conditions are chosen so that a false accept needs a coincidence rather than a nibble:
 *
 *   - the byte itself is a bool, because `enforcing` is one;
 *   - the byte after it is exactly 1, which is `initialized` on any kernel that has a live SELinux
 *     policy (it is set once at policy load and never cleared);
 *   - the eight bytes after that are all bools, which is the `policycap[]` array.
 *
 * A pointer's bytes fail the third condition unless every one of eight bytes happens to be 0 or 1.
 * A no-DEVELOP kernel fails the second unless `policycap[0]` happens to be true, and the caller is
 * required to refuse to load at all when the kernel has no `enforce` node (see README.md) - which
 * is the only check that can tell those two layouts apart with certainty, because from userspace
 * the difference is exactly the presence of that node.
 */
static inline bool rmg_layout_matches(const u8 *p, unsigned long window, unsigned long offset)
{
	const u8 *f;
	unsigned long i;

	if (offset + 2 + RMG_LAYOUT_TAIL_BOOLS > window)
		return false;

	f = p + offset;

	if (!rmg_layout_boolish(f[0]))
		return false;

	if (f[1] != 1)
		return false;

	for (i = 0; i < RMG_LAYOUT_TAIL_BOOLS; i++)
		if (!rmg_layout_boolish(f[2 + i]))
			return false;

	return true;
}

#endif /* RMG_SELINUX_LAYOUT_H */
