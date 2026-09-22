#!/usr/bin/env python3
"""Every registration the normal path makes, the dispatcher-unavailable branch makes too.

`ksu_syscall_hook_manager_init()` ends with the feature registrations, and our delta makes it return
early when `sys_call_table` could not be patched — which is the branch a Samsung target takes, because
RKP refuses that write. The branch replaces the first two registrations with its own kretprobe/kprobe
pair and, until this check existed, silently dropped the rest:

    ksu_setuid_hook_init();   -> samsung_setresuid_hook_init()
    ksu_sucompat_init();      -> samsung_sucompat_hook_init()
    ksu_avc_spoof_init();     -> (nothing)

The consequence was not a build failure or a crash. `avc_spoof` simply was not registered on those
targets, so `ksud feature check avc_spoof` answered `unsupported` and the manager's AVC spoofing switch
read as unavailable, on a device where the feature's own kprobe works — it reaches `slow_avc_audit` and
never touches a syscall.

Upstream adds registrations to that tail. The next one it adds would be dropped the same way, by a
person reading a patch rather than a compiler reading code. This is the compiler, of a sort: it reads
the patched tree and refuses a branch that is missing something.

    tools/check_rkp_branch.py --tree KernelSU     # a checkout the patch has already been applied to
    tools/check_rkp_branch.py --self-test         # prove the check can fail

Exit status is 0 when the branch is complete (or absent, which is the other way to be complete), 1 when
something is missing, 2 on an unreadable tree.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FILE = Path("kernel/hook/syscall_hook_manager.c")

# The two registrations the branch does not lose but moves: `samsung_setresuid_hook_init()` and
# `samsung_sucompat_hook_init()` call these themselves, inside the branch. Anything else in the normal
# path has no stand-in and has to be called.
MOVED_INTO_SAMSUNG = {"ksu_setuid_hook_init", "ksu_sucompat_init"}

INIT = "void __init ksu_syscall_hook_manager_init(void)"

# The registrations this check is about are named `_init`. The other things that get registered in
# here are syscall plumbing — `register_trace_prio_sys_enter`, `ksu_register_syscall_hook`, the
# `syscall_regfunc` kretprobes — and they are absent from the branch on purpose: the dispatcher being
# unavailable is exactly what they depend on, since `ksu_sys_enter_handler` returns immediately when
# there is no dispatcher to redirect to. None of them is `_init`-named, so the suffix is the rule.
REGISTERING = re.compile(r"\b([A-Za-z_]\w*_init)\s*\(")


def body_after(text: str, start: int) -> tuple[str, int] | None:
    """The `{...}` that follows `start`, and the offset just past it.

    Brace counting rather than a regex: these bodies contain string literals with braces in them, and
    the point of the check is to be right about the boundaries of the branch.
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


def registrations(code: str) -> set[str]:
    return {match.group(1) for match in REGISTERING.finditer(code)}


def analyse(text: str) -> tuple[str, set[str], set[str]]:
    """(state, in the branch, in the rest of the function) for one copy of the file."""
    start = text.find(INIT)
    if start < 0:
        return "no-init", set(), set()
    whole = body_after(text, start + len(INIT))
    if whole is None:
        return "no-init", set(), set()
    function, _ = whole

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


def self_test() -> int:
    """The two files that matter: one as it should be, one with the defect this check exists for."""

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

    print(f"self-test: complete branch {'ok' if good else 'FAILED'}")
    print(f"self-test: missing registration {'caught' if caught else 'FAILED'}")
    print(f"self-test: no branch at all {'ok' if absent_ok else 'FAILED'}")
    if good and caught and absent_ok:
        print("self-test: the check can fail, and does not fail on a correct branch")
        return 0
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tree", help="checkout the patch has been applied to")
    parser.add_argument("--self-test", action="store_true", help="run the check against fixtures")
    arguments = parser.parse_args()

    if arguments.self_test:
        return self_test()
    if not arguments.tree:
        parser.error("one of --tree or --self-test is required")

    root = Path(arguments.tree)
    path = root / FILE
    try:
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError as error:
        print(f"error: cannot read {path}: {error}", file=sys.stderr)
        return 2

    state, in_branch, rest = analyse(text)
    if state == "no-init":
        print(f"{path}: no ksu_syscall_hook_manager_init(); nothing to check")
        return 0
    if state == "no-branch":
        print(f"{path}: no dispatcher-unavailable branch, so the normal path always runs")
        return 0

    absent = missing_by(state, in_branch, rest)
    if absent:
        print(f"error: {path} returns early when the dispatcher is unavailable, and the branch does not")
        print("       register what the normal path registers:")
        for name in absent:
            print(f"         {name}()")
        print("       A Samsung target takes that branch, so on those devices the feature is not registered")
        print("       at all: `ksud feature check <name>` answers `unsupported` while the kernel is running.")
        return 1

    print(f"{path}: the early-return branch registers all {len(rest - MOVED_INTO_SAMSUNG)} of the")
    print(f"       registrations the normal path makes ({len(in_branch)} calls in the branch)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
