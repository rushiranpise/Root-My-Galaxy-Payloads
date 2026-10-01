// DFRoot's late-load LKM, built for the universal root.
//
// From https://github.com/diabl0w/DFRoot at e47ea6e ("Major refactor: Remove libc
// patching -> use insmod directly in libc++ Move most heavy lifting to custom LKM"),
// where the chain stopped patching libc, stopped exec'ing a daemon from its shellcode
// and stopped running modprobe: the shellcode now insmods this module, and this module
// does the privileged half in kernel context.
//
// Three things differ from that revision, and they are the whole of this file's
// difference from upstream:
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

#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/kprobes.h>
#include <linux/kmod.h>
#include <linux/slab.h>

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

static unsigned long kprobes_lookup(const char *name) {
	struct kprobe kp = { .symbol_name = name };
	unsigned long addr;
	if (register_kprobe(&kp) < 0) return 0;
	addr = (unsigned long)kp.addr;
	unregister_kprobe(&kp);
	return addr;
}

static int defex_pre_handler(struct kprobe *p, struct pt_regs *regs) {
	regs->regs[0] = 0;
	regs->pc = regs->regs[30];
	return 1;
}

static struct kprobe kp_user_exec = { .symbol_name = "task_defex_user_exec", .pre_handler = defex_pre_handler };
static struct kprobe kp_dc_path   = { .symbol_name = "get_dc_target_dpath",  .pre_handler = defex_pre_handler };

static int __nocfi __init dirtyfrag_init(void) {
	kallsyms_lookup_name_t kln;
	umh_setup_t umh_setup;
	umh_exec_t  umh_exec;
	unsigned long selinux;
	/* 512, not 256: the command carries two paths and the manager's package name. */
	static char cmd[512];

	kln = (kallsyms_lookup_name_t)kprobes_lookup("kallsyms_lookup_name");
	if (!kln) return -EINVAL;

	selinux = kln("selinux_state");
	if (!selinux) return -EINVAL;
	WRITE_ONCE(*(bool *)selinux, false);
	pr_info("dfroot: selinux permissive\n");

	umh_setup = (umh_setup_t)kln("call_usermodehelper_setup");
	umh_exec  = (umh_exec_t)kln("call_usermodehelper_exec");

	if (umh_setup && umh_exec) {
		static const char sh[]   = "/system/bin/sh";
		static char *envp[] = { "HOME=/", "PATH=/sbin:/vendor/bin:/system/bin", NULL };
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
			int ret;
			si->path = sh;
			register_kprobe(&kp_user_exec);
			register_kprobe(&kp_dc_path);
			ret = umh_exec(info, UMH_WAIT_PROC);
			if (kp_user_exec.addr) unregister_kprobe(&kp_user_exec);
			if (kp_dc_path.addr)   unregister_kprobe(&kp_dc_path);
			if (ret)
				pr_err("dfroot: umh_exec failed: %d\n", ret);
			else
				pr_info("dfroot: umh_exec ok\n");
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
