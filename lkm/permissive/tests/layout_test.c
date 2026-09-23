/* SPDX-License-Identifier: GPL-2.0 */
/*
 * The layout rule, exercised on the host.
 *
 * This compiles selinux_layout.h - the same header the kernel module includes - with a host
 * compiler, so the rule that guards a write into kernel memory is testable without a device, a
 * kernel tree or a module load. What it cannot test is the kernel's actual struct: those bytes come
 * from tools/btf_selinux_layout.py, which is compared against the real thing at build time.
 *
 *     make test
 */

#include <stdio.h>
#include <string.h>

#include "../selinux_layout.h"

static int checks;
static int failures;

/* Every byte starts as 0xA5, which is not a bool, so a case that forgets to set a byte it relies on
 * fails rather than passing on zero-initialised luck.
 */
static u8 window[RMG_LAYOUT_WINDOW];

static void fill(u8 base)
{
	memset(window, base, sizeof(window));
}

static void expect(const char *what, unsigned long offset, bool want)
{
	bool got = rmg_layout_matches(window, RMG_LAYOUT_WINDOW, offset);

	checks++;
	if (got == want) {
		printf("ok   %-58s %s\n", what, want ? "matches" : "refuses");
		return;
	}

	failures++;
	printf("FAIL %-58s got %s, want %s\n", what, got ? "matches" : "refuses",
	       want ? "matches" : "refuses");
}

int main(void)
{
	/* CONFIG_SECURITY_SELINUX_DEVELOP=y, enforcing, with the policycap array after it: the
	 * layout every Android kernel this is meant for has.
	 */
	fill(0);
	window[0] = 1; /* enforcing */
	window[1] = 1; /* initialized */
	window[2] = 1; /* policycap[0], netpeer is on in Android policy */
	expect("develop layout, enforcing", 0, true);

	fill(0);
	window[0] = 0; /* already permissive is still the right field */
	window[1] = 1;
	window[2] = 1;
	expect("develop layout, already permissive", 0, true);

	/* A struct whose field we were told sits elsewhere: the window test has to accept a real
	 * offset rather than only refusing everything that is not zero.
	 */
	fill(0);
	window[8] = 1;
	window[9] = 1;
	window[10] = 0;
	expect("develop layout at offset 8", 8, true);

	/* No DEVELOP: the first byte is `initialized`, and `policycap[0]` is false here, so the
	 * second condition is what stops us - writing 0 to `initialized` is the failure this rule
	 * exists to prevent.
	 */
	fill(0);
	window[0] = 1; /* initialized */
	window[1] = 0; /* policycap[0] */
	expect("no-develop layout, initialized first", 0, false);

	/* The known limit, asserted so nobody "fixes" it in the wrong direction: when policycap[0]
	 * happens to be true, a no-DEVELOP layout is indistinguishable from a DEVELOP one by shape
	 * alone. Only the caller can separate them, by refusing to load when
	 * /sys/fs/selinux/enforce does not exist. See README.md.
	 */
	fill(0);
	window[0] = 1;
	window[1] = 1;
	window[2] = 0;
	expect("no-develop layout with a true first cap (ambiguous, accepted by shape)", 0, true);

	/* Shuffled (CONFIG_RANDSTRUCT) or simply the wrong struct: a pointer's bytes after the two
	 * bools end the match.
	 */
	fill(0);
	window[0] = 1;
	window[1] = 1;
	window[2] = 0xff; /* a pointer's high byte */
	window[3] = 0xff;
	expect("pointer bytes follow the two bools", 0, false);

	/* The candidate byte itself is not a bool. */
	fill(0);
	window[0] = 0x80;
	window[1] = 1;
	expect("candidate byte is not a bool", 0, false);

	/* `initialized` false: SELinux is not initialised, so there is nothing to make permissive and
	 * the field we were pointed at is not the one we think it is.
	 */
	fill(0);
	window[0] = 1;
	window[1] = 0;
	expect("initialized is false", 0, false);

	/* Offsets that would read past the window, including the two that pass a naive bounds test:
	 * one byte short, and one byte short of the tail.
	 */
	fill(0);
	window[0] = 1;
	window[1] = 1;
	expect("offset at the very end of the window", RMG_LAYOUT_WINDOW, false);
	expect("offset leaving no room for the tail",
	       RMG_LAYOUT_WINDOW - 2 - RMG_LAYOUT_TAIL_BOOLS + 1, false);

	printf("\n%d checks, %d failures\n", checks, failures);

	return failures ? 1 : 0;
}
