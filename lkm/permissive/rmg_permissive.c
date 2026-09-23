// SPDX-License-Identifier: GPL-2.0
/*
 * rmg_permissive - put SELinux into permissive mode by writing one byte of kernel data.
 *
 * This is this project's own module, written from the kernel's own headers and the public
 * documentation of `struct selinux_state`; no third-party module source was copied into it. The
 * technique it uses - writing a data field rather than patching kernel text - is not ours and is not
 * claimed: it is the reason a Samsung kernel with RKP can accept this at all. RKP pins kernel text
 * at EL2, which is why our text-patching selinux_hide faults with -38 on this hardware, while a
 * data byte stays writable. Credits and the reasoning are in README.md.
 *
 * Why this exists
 * ---------------
 * `setenforce 0` needs two things to go right: the policy has to allow the caller to write
 * /sys/fs/selinux/enforce, and the SELinux filesystem has to answer honestly. On this device both
 * have failed for us - our own selinux_hide forge is one of the reasons, and it is also what denies
 * the su domain the ctl.* property writes that KernelSU's own soft reboot needs. A kernel-side write
 * bypasses the policy and every userspace path, so it works when the sysfs route does not.
 *
 * The contract with the caller
 * ----------------------------
 * The write is the product; the module has no reason to stay loaded afterwards, and staying loaded
 * is a detector signal (`/proc/modules`). So on success init returns -E2BIG on purpose: the kernel
 * does not finish loading the module, nothing is left in /proc/modules, and the write has already
 * happened. A caller must treat that errno as "the write landed", and the line this module prints is
 * the record of what happened:
 *
 *     rmg_permissive: result=applied offset=0 before=1 after=0 symbol=selinux_state value=0
 *
 * Every failure path logs result=refused with a reason and returns a different errno, so the log
 * distinguishes a refused write from a write that happened. `stay=1` keeps the module loaded
 * instead, for a caller that would rather see init_module return 0.
 *
 * Preconditions the caller owns
 * -----------------------------
 *  1. /sys/fs/selinux/enforce must exist. It exists only when CONFIG_SECURITY_SELINUX_DEVELOP is
 *     on, and when it is off there is no `enforcing` field to write - the offset would name
 *     `initialized` and clearing it would tell the kernel SELinux was never initialised. The module
 *     cannot see that config, so the caller checks the node before loading. Nothing else in this
 *     design can tell those two layouts apart with certainty.
 *  2. Passing `offset` is preferred over trusting the built-in stamp whenever the caller can derive
 *     it - tools/btf_selinux_layout.py reads the same value out of /sys/kernel/btf/vmlinux.
 */

#include <linux/errno.h>
#include <linux/kallsyms.h>
#include <linux/module.h>
#include <linux/printk.h>
#include <linux/string.h>
#include <linux/types.h>

#include "selinux_layout.h"

/*
 * Byte offset of `selinux_state.enforcing`, stamped at build time by CI from the kernel tree the
 * module is built against (`.github/workflows/lkm-build.yml`, tools/btf_selinux_layout.py). -1 means
 * "nobody derived it", and then only an explicit `offset=` works - a module that guesses is worse
 * than one that refuses.
 */
#ifndef RMG_ENFORCING_OFFSET
#define RMG_ENFORCING_OFFSET (-1)
#endif

static int offset = RMG_ENFORCING_OFFSET;
module_param(offset, int, 0400);
MODULE_PARM_DESC(offset,
	"Byte offset of selinux_state.enforcing; -1 (the stamped default) refuses to write");

static int value;
module_param(value, int, 0400);
MODULE_PARM_DESC(value, "Byte value to write: 0 is permissive, 1 is enforcing");

static bool dry_run;
module_param(dry_run, bool, 0400);
MODULE_PARM_DESC(dry_run, "Check the layout and report, but do not write");

static bool stay;
module_param(stay, bool, 0400);
MODULE_PARM_DESC(stay, "Return success and stay loaded, instead of unloading after the write");

static char symbol[64] = "selinux_state";
module_param_string(symbol, symbol, sizeof(symbol), 0400);
MODULE_PARM_DESC(symbol, "Symbol to write into");

struct rmg_symbol_probe {
	const char *name;
	unsigned long addr;
};

/* kallsyms_on_each_symbol walks every symbol in name order; the strcmp is ours, and a match stops
 * the walk by returning non-zero. The symbol is not exported, which is exactly why it is resolved
 * this way: kallsyms_on_each_symbol is EXPORT_SYMBOL_GPL, so a GPL module can reach any symbol
 * without the kprobe trick that older out-of-tree modules used.
 */
static int rmg_symbol_cb(void *data, const char *name, unsigned long addr)
{
	struct rmg_symbol_probe *probe = data;

	if (probe->addr)
		return 1;

	if (!strcmp(name, probe->name))
		probe->addr = addr;

	return 0;
}

static int __init rmg_permissive_init(void)
{
	struct rmg_symbol_probe probe = { .name = symbol, .addr = 0 };
	const u8 *base;
	u8 *field;
	u8 before;

	if (offset < 0) {
		pr_err("rmg_permissive: result=refused reason=no-offset (build stamped %d, pass offset=)\n",
		       RMG_ENFORCING_OFFSET);
		return -EINVAL;
	}

	if (offset + 2 + RMG_LAYOUT_TAIL_BOOLS > RMG_LAYOUT_WINDOW) {
		pr_err("rmg_permissive: result=refused reason=offset-out-of-window offset=%d\n",
		       offset);
		return -EINVAL;
	}

	if (value < 0 || value > 1) {
		pr_err("rmg_permissive: result=refused reason=value-not-a-bool value=%d\n", value);
		return -EINVAL;
	}

	kallsyms_on_each_symbol(rmg_symbol_cb, &probe);
	if (!probe.addr) {
		pr_err("rmg_permissive: result=refused reason=no-symbol symbol=%s\n", symbol);
		return -ENOENT;
	}

	base = (const u8 *)probe.addr;
	if (!rmg_layout_matches(base, RMG_LAYOUT_WINDOW, (unsigned long)offset)) {
		/*
		 * The bytes here are not the layout we were told to expect: a shuffled layout, a
		 * different struct, or an offset derived from another kernel. Writing blind would put
		 * a byte inside whatever field really lives at this offset, so nothing is written.
		 */
		pr_err("rmg_permissive: result=refused reason=layout-mismatch symbol=%s offset=%d bytes=%02x %02x %02x %02x\n",
		       symbol, offset, base[offset], base[offset + 1], base[offset + 2],
		       base[offset + 3]);
		return -EINVAL;
	}

	field = (u8 *)(probe.addr + (unsigned long)offset);
	before = READ_ONCE(*field);

	if (!dry_run && before != (u8)value)
		WRITE_ONCE(*field, (u8)value);

	pr_info("rmg_permissive: result=%s offset=%d before=%u after=%u symbol=%s value=%d\n",
		dry_run ? "dry-run" : (before == (u8)value ? "noop" : "applied"), offset, before,
		READ_ONCE(*field), symbol, value);

	if (!dry_run && !rmg_layout_matches(base, RMG_LAYOUT_WINDOW, (unsigned long)offset)) {
		/*
		 * A post-condition, not a second guess: `initialized` is the byte after the one we
		 * wrote, so if the shape no longer holds, the write went somewhere it should not have.
		 * Nothing here can undo that, but the log says so rather than reporting success.
		 */
		pr_err("rmg_permissive: result=verify-failed offset=%d after=%u\n", offset,
		       READ_ONCE(*field));
		return -EIO;
	}

	if (stay)
		return 0;

	/*
	 * -E2BIG after a successful write is deliberate. The module is not needed once the byte is
	 * written, and a module that is not in /proc/modules is a module no detector can see. The
	 * caller reads the log line above to know the write landed.
	 */
	return -E2BIG;
}

/* Only reachable with `stay=1`: without an exit function a loaded module could never be removed. */
static void __exit rmg_permissive_exit(void)
{
	pr_info("rmg_permissive: unloaded; the byte it wrote stays until the kernel reinitialises it\n");
}

module_init(rmg_permissive_init);
module_exit(rmg_permissive_exit);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Root My Galaxy");
MODULE_DESCRIPTION("Write one validated byte of kernel data (selinux_state.enforcing)");
MODULE_VERSION("0.1");
