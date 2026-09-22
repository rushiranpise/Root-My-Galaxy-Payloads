#!/usr/bin/env python3
"""The two shortcuts our delta takes must not drop work the full path does.

Both of these replace upstream's normal path, and both dropped something silently. Neither showed up as
a build failure or a crash: the work was simply absent, and the driver reported the absence in a way
that looked like a platform limitation.

**The dispatcher-unavailable branch.** `ksu_syscall_hook_manager_init()` ends with the feature
registrations, and our delta makes it return early when `sys_call_table` could not be patched — the
branch a Samsung target takes, because RKP refuses that write. The branch replaces the first two
registrations with its own kretprobe/kprobe pair and silently dropped the third:

    ksu_setuid_hook_init();   -> samsung_setresuid_hook_init()
    ksu_sucompat_init();      -> samsung_sucompat_hook_init()
    ksu_avc_spoof_init();     -> (nothing)

So `avc_spoof` was never registered on those targets: `ksud feature check avc_spoof` answered
`unsupported`, the manager's switch read as unavailable, and the feature's own kprobe — which reaches
`slow_avc_audit` and never touches a syscall — had nothing to run it.

**The late-load path.** `dispatch.c` skips the boot-completed event when the module was loaded after
boot, which is how this delta reaches a locked device, so `on_boot_completed()` never runs for one.
That function is where `ksu_avc_spoof_late_init()` arms the hook, so on a late-loaded module the feature
was registered and enabled and yet never armed. `kernel/core/init.c` therefore calls it on the late-load
path, and `REQUIRED_ON_LATE_LOAD` below is what must stay there.

`--tree` checks a patched checkout for both. The second one also *reports* the work a late-loaded module
still skips, because the two paths are never going to match exactly and the difference should be visible
rather than remembered.

    tools/check_rkp_branch.py --tree KernelSU     # a checkout the patch has already been applied to
    tools/check_rkp_branch.py --self-test         # prove both checks can fail

Exit status is 0 when both shortcuts are complete (or absent, which is the other way to be complete), 1
when something is missing, 2 on an unreadable tree.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FILE = Path("kernel/hook/syscall_hook_manager.c")
BOOT_EVENT = Path("kernel/runtime/boot_event.c")
CORE_INIT = Path("kernel/core/init.c")

# The two registrations the branch does not lose but moves: `samsung_setresuid_hook_init()` and
# `samsung_sucompat_hook_init()` call these themselves, inside the branch. Anything else in the normal
# path has no stand-in and has to be called.
MOVED_INTO_SAMSUNG = {"ksu_setuid_hook_init", "ksu_sucompat_init"}

INIT = "void __init ksu_syscall_hook_manager_init(void)"
LATE_BRANCH = "if (ksu_late_loaded) {"
BOOT_COMPLETED = "void on_boot_completed(void)"

# What a late-loaded module has to do as well, because the event that would normally do it is skipped.
# `ksu_avc_spoof_late_init()` sets the feature's own boot-completed flag and arms its kprobe; without
# it the switch reads enabled and nothing is hooked.
REQUIRED_ON_LATE_LOAD = {"ksu_avc_spoof_late_init"}

# The registrations the first check is about are named `_init`. The other things that get registered in
# that function are syscall plumbing — `register_trace_prio_sys_enter`, `ksu_register_syscall_hook`,
# the `syscall_regfunc` kretprobes — and they are absent from the branch on purpose: the dispatcher
# being unavailable is exactly what they depend on, since `ksu_sys_enter_handler` returns immediately
# when there is no dispatcher to redirect to. None of them is `_init`-named, so the suffix is the rule.
REGISTERING = re.compile(r"\b([A-Za-z_]\w*_init)\s*\(")

# Every call, for the late-load comparison. Logging and control flow are not work: `pr_info` says a
# thing happened, it does not do it, and it is not worth reporting as skipped.
CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
NOT_WORK = {"if", "for", "while", "switch", "return", "sizeof", "typeof", "min", "max"}


def body_after(text: str, start: int) -> tuple[str, int] | None:
    """The `{...}` that follows `start`, and the offset just past it.

    Brace counting rather than a regex: these bodies contain string literals with braces in them, and
    the point of the check is to be right about the boundaries of the code it is reading.
    """
    opening = text.find("{", start)
    if opening < 0:
        return None
    depth = 0
    for index in range(opening, len(text)):
        character = text[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[opening : index + 1], index + 1
    return None


def function_body(text: str, signature: str) -> str | None:
    """The body `{...}` that follows a declaration or control statement.

    The brace is searched from the *start* of the signature, so `if (x) {` hands back the statement's
    own body rather than the first block nested inside it — which is the mistake this function exists
    to not make.
    """
    start = text.find(signature)
    if start < 0:
        return None
    body = body_after(text, start)
    return body[0] if body else None


def registrations(code: str) -> set[str]:
    return {match.group(1) for match in REGISTERING.finditer(code)}


def calls(code: str) -> set[str]:
    return {
        name
        for name in (match.group(1) for match in CALL.finditer(code))
        if not name.startswith("pr_") and name not in NOT_WORK
    }


def analyse(text: str) -> tuple[str, set[str], set[str]]:
    """(state, in the branch, in the rest of the function) for one copy of the file."""
    function = function_body(text, INIT)
    if function is None:
        return "no-init", set(), set()

    branch_start = function.find("ksu_dispatcher_nr < 0")
    if branch_start < 0:
        return "no-branch", set(), set()
    branch = body_after(function, branch_start)
    if branch is None:
        return "no-branch", set(), set()
    branch_code, past = branch

    rest = function[:branch_start] + function[past:]
    return "checked", registrations(branch_code), registrations(rest)


def missing_by(state: str, in_branch: set[str], rest: set[str]) -> list[str]:
    if state != "checked":
        return []
    return sorted(rest - in_branch - MOVED_INTO_SAMSUNG)


def late_load(boot_event_text: str, init_text: str) -> tuple[set[str], set[str], list[str]]:
    """(called on the late path, skipped there, required and missing).

    The requirement is read out of the tree's own `on_boot_completed()` rather than asserted blindly:
    a tree whose event arms nothing — the tiann leg, whose `on_boot_completed()` has no spoof call —
    has nothing to lose on the late path, and demanding the call there would fail a correct tree.
    """
    completed = function_body(boot_event_text, BOOT_COMPLETED)
    branch = function_body(init_text, LATE_BRANCH)
    if completed is None or branch is None:
        return set(), set(), []
    done = calls(branch)
    skipped = calls(completed) - done
    required = REQUIRED_ON_LATE_LOAD & calls(completed)
    return done, skipped, sorted(required - done)


def self_test() -> int:
    """The fixtures that matter: each check correct, and each able to fail on the defect it exists for."""

    def function(branch_extra: str) -> str:
        return f"""{INIT}
{{
    int ret;

    if (ksu_dispatcher_nr < 0) {{
        pr_warn("dispatcher unavailable\\n");
#if defined(CONFIG_KSU_SAMSUNG_RKP)
        samsung_setresuid_hook_init();
        ret = samsung_sucompat_hook_init();
{branch_extra}#endif
        return;
    }}

    ksu_setuid_hook_init();
    ksu_sucompat_init();
    ksu_avc_spoof_init();
}}
"""

    def init(branch_extra: str) -> str:
        return f"""int __init kernelsu_init(void)
{{
    if (ksu_late_loaded) {{
        ksu_syscall_hook_manager_init();
        ksu_boot_completed = true;
{branch_extra}        track_throne(false);
    }} else {{
        ksu_syscall_hook_manager_init();
    }}
}}
"""

    complete = function("        ksu_avc_spoof_init();\n")
    defective = function("")

    state, in_branch, rest = analyse(complete)
    good = state == "checked" and not missing_by(state, in_branch, rest)

    state, in_branch, rest = analyse(defective)
    caught = missing_by(state, in_branch, rest) == ["ksu_avc_spoof_init"]

    # A tree where the dispatcher always installs: nothing to check, and not a failure, because the
    # normal path is the whole path then.
    state, in_branch, rest = analyse(function("").replace("if (ksu_dispatcher_nr < 0)", "if (0)"))
    absent_ok = state == "no-branch"

    # The shape of the real one: three things, one of which the late path has to repeat.
    boot_event = f"""{BOOT_COMPLETED}
{{
    ksu_boot_completed = true;
    pr_info("on_boot_completed!\\n");
    track_throne(true);
    ksu_selinux_hide_drop_backup_if_unused();
    ksu_avc_spoof_late_init();
}}
"""

    _, _, missing = late_load(boot_event, init("        ksu_avc_spoof_late_init();\n"))
    late_ok = not missing

    _, _, missing = late_load(boot_event, init(""))
    late_caught = missing == ["ksu_avc_spoof_late_init"]

    # A tree whose event arms nothing has nothing to lose here, and must not be failed for it.
    _, _, missing = late_load(boot_event.replace("    ksu_avc_spoof_late_init();\n", ""), init(""))
    late_absent_ok = not missing

    # The rest of the difference is reported, not failed on: the two paths will never match exactly.
    _, skipped, missing = late_load(
        boot_event.replace("    track_throne(true);\n", "    track_throne(true);\n    ksu_ksud_init();\n"),
        init("        ksu_avc_spoof_late_init();\n"),
    )
    reported = "ksu_ksud_init" in skipped and not missing

    results = [
        ("complete branch", good),
        ("missing registration", caught),
        ("no branch at all", absent_ok),
        ("late load arms", late_ok),
        ("late load missing the arming", late_caught),
        ("event that arms nothing", late_absent_ok),
        ("skipped work is reported", reported),
    ]
    for label, passed in results:
        print(f"self-test: {label} {'ok' if passed else 'FAILED'}")
    if all(passed for _, passed in results):
        print("self-test: both checks can fail, and neither fails on a correct tree")
        return 0
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tree", help="checkout the patch has been applied to")
    parser.add_argument("--self-test", action="store_true", help="run the checks against fixtures")
    arguments = parser.parse_args()

    if arguments.self_test:
        return self_test()
    if not arguments.tree:
        parser.error("one of --tree or --self-test is required")

    root = Path(arguments.tree)

    def read(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8", errors="surrogateescape")
        except OSError as error:
            print(f"error: cannot read {error.filename}: {error}", file=sys.stderr)
            return None

    text = read(root / FILE)
    if text is None:
        return 2

    status = 0
    state, in_branch, rest = analyse(text)
    if state == "no-init":
        print(f"{root / FILE}: no ksu_syscall_hook_manager_init(); nothing to check there")
    elif state == "no-branch":
        print(f"{root / FILE}: no dispatcher-unavailable branch, so the normal path always runs")
    else:
        absent = missing_by(state, in_branch, rest)
        if absent:
            print(f"error: {root / FILE} returns early when the dispatcher is unavailable, and that")
            print("       branch does not register what the normal path registers:")
            for name in absent:
                print(f"         {name}()")
            print("       A Samsung target takes that branch, so the feature is not registered at all")
            print("       there: `ksud feature check <name>` answers `unsupported` while the kernel runs.")
            status = 1
        else:
            print(f"{root / FILE}: the early-return branch registers all"
                  f" {len(rest - MOVED_INTO_SAMSUNG)} of the")
            print(f"       registrations the normal path makes ({len(in_branch)} calls in the branch)")

    boot = read(root / BOOT_EVENT)
    core = read(root / CORE_INIT)
    if boot is None or core is None:
        return 2

    done, skipped, missing = late_load(boot, core)
    if missing:
        print(f"error: a late-loaded module never runs on_boot_completed() — dispatch.c skips the event —")
        print(f"       and the late-load branch of {CORE_INIT} does not call:")
        for name in missing:
            print(f"         {name}()")
        print("       The feature is registered and its switch reads enabled, but it is never armed.")
        status = 1
    elif not done:
        print(f"{root / CORE_INIT}: no late-load branch to check")
    else:
        print(f"{root / CORE_INIT}: the late-load branch does {len(done)} things")
        if skipped:
            print("       Work on_boot_completed() does that a late-loaded module skips:")
            for name in sorted(skipped):
                print(f"         {name}()")
    return status


if __name__ == "__main__":
    sys.exit(main())
