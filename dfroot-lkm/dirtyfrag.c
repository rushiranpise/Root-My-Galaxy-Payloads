// DFRoot's late-load LKM, built for the universal root.
//
// From https://github.com/diabl0w/DFRoot at e47ea6e ("Major refactor: Remove libc
// patching -> use insmod directly in libc++ Move most heavy lifting to custom LKM"),
// updated to 23085ef ("Fix DEFEX hook on some devices").
//
// Three things differ from upstream, and they are the whole of this file's difference:
//
//   1. the daemon path. Upstream names their own package (`/data/user_de/0/df.root/ksud`);
//      the chain reads the daemon the app stages, which is under this app's id.
//   2. `package_name` is a module parameter rather than a compiled-in literal. This
//      project's chain drives one library for three KernelSU projects, and the manager
//      the daemon is told to serve is a value the app chooses per run - a literal here
//      would grant root to whichever manager upstream ships.
//   3. the `--ro-partitions` and `--soft-reboot` arguments are not passed. Both are
//      options of upstream's KernelSU fork rather than of the daemons this project's
//      payload repository builds, and both behaviours already exist in the app (the
//      read-only partition wall, and Auto soft restart) - so passing them would make
//      the daemon fail to parse its own command line.
//
// And one thing this chain has to do that upstream's does not: **stage the daemon**.
//
// The daemon built for this project runs `late-load` in two halves. Its first act is to
// rename `/data/local/tmp/.ksud-stage` onto `/data/adb/ksud`, because the install has to
// happen before loading the module changes this process's security context; that file is
// the daemon's own bytes, put there by whoever wants it installed. The payload flow
// stages it as part of every run, and an install **consumes** it - so a universal run,
// which is the other flow entirely, found nothing to rename and exited non-zero before
// it did anything at all. That is what `/dev/dfm1` was saying.
//
// So this command writes it as well, from the daemon the app staged: this runs as root,
// with the app's data directory readable, which is the one place that can. The same
// three facts the payload flow's staging pass keeps apply here - the file is the daemon
// this run resolved, it is executable, and it is where that daemon's own `late-load`
// looks for it.
//
// The markers are upstream's and unchanged: /dev/dfm0 for "late-load completed",
// /dev/dfm1 for "it did not", which is what the chain's parent side reports on. The
// daemon's own output goes to a file beside the stage file, because a usermode helper
// has no stdout: without that redirect a refusal is a bare non-zero exit code, which is
// what made the staging bug above take a whole diagnostic round to find.
//
// ## What `23085ef` changed, and the one place this file still differs in kind
//
// Upstream's fix is the hook itself: `task_defex_enforce` instead of `get_dc_target_dpath`.
// The path-based hook was the DEFEX entry point on the kernels it was written against, and
// on others the enforcement runs through `task_defex_enforce`, so the chain's `insmod`
// was refused on those devices with nothing in the log to say why - the hook registered
// cleanly and was simply never called. Both this file and upstream now hook
// `task_defex_enforce` *and* `task_defex_user_exec`, log which of them registered, and
// carry on when one is missing: a kernel that names only one of the two still roots, and a
// kernel that names neither says so in dmesg instead of failing silently.
//
// The remaining difference in kind is about ordering, and it is deliberate. Upstream
// registers both DEFEX probes first and its usermode-helper lookups after, so a device
// whose `call_usermodehelper_setup`/`_exec` are not exported returns `-EINVAL` from init
// with two probes still registered - and an init that returns an error unloads the module,
// leaving the probes pointing into memory that is no longer there. Here the probes are
// registered immediately before the exec and removed immediately after it, so the window
// in which they exist is exactly the window in which the daemon's transition needs them,
// and a missing helper symbol leaves nothing behind.

#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/kmod.h>
#include <linux/kprobes.h>
#include <linux/module.h>
#include <linux/ptrace.h>

typedef unsigned long (*kallsyms_lookup_name_t)(const char *name);
typedef void *(*umh_setup_t)(const char *path, char **argv, char **envp, gfp_t gfp,
			     void *init, void *cleanup, void *data);
typedef int (*umh_exec_t)(void *info, int wait);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("DFRoot LKM");

/* The manager the daemon is told to serve. The app passes the flavour's package. */
static char package_name[64] = "me.weishu.kernelsu";
module_param_string(package_name, package_name, sizeof(package_name), 0);

/* The daemon this chain stages and runs, and where its own `late-load` looks for it. */
#define DAEMON "/data/user_de/0/dev.rushiranpise.rmgnext/ksud"
#define STAGE "/data/local/tmp/.ksud-stage"
#define DAEMON_LOG "/data/local/tmp/dfroot-ksud.log"

static int defex_pre_handler(struct kprobe *p, struct pt_regs *regs)
{
	(void)p;
	regs->regs[0] = 0;	   /* x0 = DEFEX_ALLOW */
	regs->pc = regs->regs[30]; /* skip body: return to caller */
	return 1;
}

static int register_defex_hook(struct kprobe *kp, const char *name, unsigned long addr)
{
	*kp = (struct kprobe){ .addr = (kprobe_opcode_t *)addr,
			       .pre_handler = defex_pre_handler };
	if (register_kprobe(kp) < 0) {
		pr_err("dfroot: %s not hooked\n", name);
		return -1;
	}
	pr_info("dfroot: %s hooked\n", name);
	return 0;
}

static int __nocfi __init dirtyfrag_init(void)
{
	kallsyms_lookup_name_t kln;
	umh_setup_t umh_setup;
	umh_exec_t  umh_exec;
	bool *selinux_state;
	struct kprobe kln_kp;
	struct kprobe enforce_kp = { 0 };
	struct kprobe user_exec_kp = { 0 };
	/* 512, not 256: the command carries two paths and the manager's package name. */
	static char cmd[512];

	kln_kp = (struct kprobe){ .symbol_name = "kallsyms_lookup_name" };
	if (register_kprobe(&kln_kp) < 0) {
		pr_err("dfroot: kallsyms_lookup_name not found\n");
		return -EINVAL;
	}
	kln = (kallsyms_lookup_name_t)kln_kp.addr;
	unregister_kprobe(&kln_kp);

	selinux_state = (bool *)kln("selinux_state");
	if (!selinux_state) {
		pr_err("dfroot: selinux_state not found\n");
		return -EINVAL;
	}
	WRITE_ONCE(*selinux_state, false);
	pr_info("dfroot: selinux permissive\n");

	umh_setup = (umh_setup_t)kln("call_usermodehelper_setup");
	umh_exec  = (umh_exec_t)kln("call_usermodehelper_exec");

	if (umh_setup && umh_exec) {
		static const char sh[]   = "/system/bin/sh";
		static char *envp[] = { "HOME=/" , "PATH=/sbin:/vendor/bin:/system/bin", NULL };
		static char *argv[] = { (char *)sh, "-c", cmd, NULL };
		void *info;

		/*
		 * Stage, then load.
		 *
		 * `cp` rather than `mv`: the daemon the app staged is the only copy of those bytes, and a run
		 * that failed before `late-load` renamed the stage file would otherwise have left the app with
		 * nothing to stage again. The daemon renames it itself as its first act, which is what consumes
		 * it.
		 */
		snprintf(cmd, sizeof(cmd),
			 "cp %s %s && chmod 0755 %s"
			 " && %s late-load --package-name %s > %s 2>&1"
			 " && touch /dev/dfm0 || touch /dev/dfm1",
			 DAEMON, STAGE, STAGE,
			 DAEMON, package_name, DAEMON_LOG);

		info = umh_setup(sh, argv, envp, GFP_KERNEL, NULL, NULL, NULL);
		if (info) {
			struct subprocess_info *si = (struct subprocess_info *)info;
			unsigned long enforce = kln("task_defex_enforce");
			unsigned long user_exec = kln("task_defex_user_exec");
			int have_enforce, have_user_exec;
			int ret;

			/* bypass CONFIG_STATIC_USERMODEHELPER_PATH="" overriding path to "" */
			si->path = sh;

			/*
			 * Both DEFEX entry points this kernel has are hooked, and a kernel that has only one
			 * still roots: which of the two the enforcement runs through is a property of the
			 * build, and upstream's own note on the fix says exactly that.
			 */
			have_enforce = enforce ? (register_defex_hook(&enforce_kp,
								      "task_defex_enforce", enforce) == 0) : 0;
			have_user_exec = user_exec ? (register_defex_hook(&user_exec_kp,
									  "task_defex_user_exec", user_exec) == 0) : 0;

			ret = umh_exec(info, UMH_WAIT_PROC);
			if (ret)
				pr_err("dfroot: umh_exec failed: %d\n", ret);
			else
				pr_info("dfroot: umh_exec ok\n");

			if (have_enforce) unregister_kprobe(&enforce_kp);
			if (have_user_exec) unregister_kprobe(&user_exec_kp);
		} else {
			pr_err("dfroot: umh_setup returned NULL\n");
		}
	} else {
		pr_err("dfroot: umh symbols missing (setup=%px exec=%px)\n", umh_setup, umh_exec);
	}

	/* Return random error to unload module. */
	return -E2BIG;
}

/* No module_exit: we never unload; saves .exit sections. */
module_init(dirtyfrag_init);
