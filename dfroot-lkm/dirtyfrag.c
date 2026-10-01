// DFRoot's late-load LKM, built for the universal root.
//
// From https://github.com/diabl0w/DFRoot at e47ea6e ("Major refactor: Remove libc
// patching -> use insmod directly in libc++ Move most heavy lifting to custom LKM").
//
// Four things differ from upstream, and they are the whole of this file's difference:
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
//   4. the DEFEX hook pair, which is the subject of its own note below: this hooks
//      `task_defex_user_exec` **and `get_dc_target_dpath`**, where upstream's current
//      revision (`23085ef`, "Fix DEFEX hook on some devices") hooks
//      `task_defex_user_exec` and `task_defex_enforce` instead.
//
// ## Why the hook upstream replaced is the hook this project keeps
//
// `23085ef` moved the hook because the path-based function is not the enforcement entry
// point on every kernel. It is on the one this project was built against, and the
// replacement was tried here: with the module built from that revision the chain still
// patched both files and still triggered the loader, and then failed at the last step -
// `/data/local/tmp/dfroot-ksud.log` was created root-owned and **empty**, and
// `/data/local/tmp/.ksud-stage` had been consumed, which says the daemon started from the
// data path and was killed before it wrote a line. Two of three runs also hung the phone
// and rebooted it. That is DEFEX refusing an exec out of a data path, which is the check
// the path-based hook is for, and it is also why `task_defex_enforce` is not hooked here
// even alongside it: hooking it is the only other difference between the two builds, and
// a kernel that skips an enforcement function it expected to run is the plausible source
// of the hang. Neither is worth a phone for a fix aimed at kernels this project has no
// hardware for.
//
// So the pairing is: bypass the path check (that is what lets a daemon in the app's data
// directory run at all), and bypass the user-exec entry that calls it. A device whose
// kernel has no `get_dc_target_dpath` gets a clean refusal - `register_kprobe` says so in
// dmesg and the chain reports `/dev/dfm1` - rather than a probe on a function nobody has
// tested here.
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
// looks for it. The command writes the daemon's own output to a file with it, because a
// usermode helper has no stdout and a refusal is otherwise a bare non-zero exit code.
//
// The markers are upstream's and unchanged: /dev/dfm0 for "late-load completed",
// /dev/dfm1 for "it did not", which is what the chain's parent side reports on.
//
// ## What this file did take from `23085ef`
//
// The probe registration and the logging, which is how the failure above could be read at
// all: the symbol lookup is done explicitly so a missing one is named in dmesg instead of
// being silent, both entry points are probed only around the daemon's exec, and the module
// says which of them it hooked.
//
// The ordering is this project's, and it is deliberate. Upstream registers its probes
// first and looks the usermode-helper symbols up after, so a device whose
// `call_usermodehelper_setup`/`_exec` are not exported returns `-EINVAL` from init with
// two probes still registered - and an init that returns an error unloads the module,
// leaving the probes pointing into memory that is no longer there. Here they are
// registered immediately before the exec and removed immediately after it.

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

static int __nocfi __init hook_defex(struct kprobe *kp, const char *name,
				     unsigned long addr)
{
	if (!addr) {
		pr_err("dfroot: %s not found\n", name);
		return -1;
	}
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
	struct kprobe user_exec_kp = { 0 };
	struct kprobe dc_path_kp = { 0 };
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
			int have_user_exec, have_dc_path;
			int ret;

			/* bypass CONFIG_STATIC_USERMODEHELPER_PATH="" overriding path to "" */
			si->path = sh;

			/*
			 * The two entry points this project has hardware for, probed around the exec rather
			 * than for the life of the module - see the note at the top of the file for why the
			 * path-based one is not the one upstream replaced it with.
			 */
			have_user_exec = hook_defex(&user_exec_kp, "task_defex_user_exec",
						    kln("task_defex_user_exec")) == 0;
			have_dc_path = hook_defex(&dc_path_kp, "get_dc_target_dpath",
						  kln("get_dc_target_dpath")) == 0;

			ret = umh_exec(info, UMH_WAIT_PROC);
			if (ret)
				pr_err("dfroot: umh_exec failed: %d\n", ret);
			else
				pr_info("dfroot: umh_exec ok\n");

			if (have_user_exec) unregister_kprobe(&user_exec_kp);
			if (have_dc_path) unregister_kprobe(&dc_path_kp);
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
