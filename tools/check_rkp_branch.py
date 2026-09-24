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

Both this and the registration above are read out of a *patched checkout* by `--tree`, which every build
job does. That leaves one way for the arming to go missing again unpunished: the published patches are
per flavour and ref, and only the ref a run asks for is ever applied - so the 3.4.0 patch is unguarded
until somebody builds `v3.4.0`, however long that is. `--patches` closes that by reading the diffs
themselves, which needs no checkout and no network at all.

**The setuid hand-off.** The kretprobe stands in for the dispatcher's `ksu_hook_setresuid()`, which knew
both uids - the one the process was leaving (`old_uid`, captured before the call) and the one it moved to
(`current_uid()` after it) - and passed them to the tree's setuid entry point in the order *that tree*
declares. The order is not the same everywhere. KernelSU and KernelSU-Next define
`ksu_handle_setresuid(uid_t old_uid, uid_t new_uid)` and call it `(captured_old, current)`. ReSukiSU kept
the older `ksu_handle_setresuid(uid_t ruid, uid_t euid, uid_t suid)` - the manual-hook entry point, whose
`ruid` is the uid being moved *to* and whose body reads the uid being left from `current_cred()` itself.
Copying the second call into the first tree's delta reverses the pair, and the reversal is silent in the
worst way: `ksu_handle_setuid()` then sees every app spawn as `new_uid = 0`, so `ksu_is_manager_uid()`
never matches, the manager branch never runs, no manager is ever handed the driver fd - while root, which
arrives through the execve kprobe, keeps working. The manager app reads that absence as "not installed"
(`KernelSU: -1`, `LKM: false`) on a phone whose kernel module is loaded and answering.

`--tree` checks a patched checkout for all three. The second also *reports* the work a late-loaded module
still skips, because the two paths are never going to match exactly and the difference should be visible
rather than remembered; the third resolves the order out of the tree rather than asserting one, so it
passes on both conventions and fails only on the mismatch.

    tools/check_rkp_branch.py --tree KernelSU     # a checkout the patch has already been applied to
    tools/check_rkp_branch.py --patches kernelsu/patches   # every published patch, with no checkout
    tools/check_rkp_branch.py --self-test         # prove every check can fail

Exit status is 0 when every shortcut is complete (or absent, which is the other way to be complete), 1
when something is missing, 2 on an unreadable tree or an unreadable patch.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FILE = Path("kernel/hook/syscall_hook_manager.c")
BOOT_EVENT = Path("kernel/runtime/boot_event.c")
CORE_INIT = Path("kernel/core/init.c")
SETUID = Path("kernel/hook/setuid_hook.c")

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

# Where the published patches live, and how to find one file section and one hunk inside a diff.
# Read by section and hunk rather than as text: what a patch *adds* is the thing to check, and a hunk
# of another file can say the same words in its context.
PATCHES = Path("kernelsu/patches")
DIFF_SECTION = re.compile(r"^diff --git a/(\S+) b/(\S+)\s*$", re.M)
HUNK_HEADER = re.compile(r"^@@", re.M)

# The feature as a patch names it, the call that arms it on the late path, and the line the upstream
# late-load branch sets - which is what tells that hunk from every other hunk of the same file.
SPOOF = "ksu_avc_spoof"
LATE_ARM = "ksu_avc_spoof_late_init"
LATE_BRANCH_CUE = "ksu_boot_completed = true"

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

# The task work that forwards the kretprobe's capture into the tree's setuid engine.
TASK_WORK = "static void setresuid_task_work_func(struct callback_head *callback)"

# The primitive the setuid handling is built on, and the entry point the manual-hook integration - and
# therefore this delta - is meant to call. In some trees the second is a wrapper around the first; in
# others it is the engine itself.
PRIMITIVE = "ksu_handle_setuid"
ENTRY = "ksu_handle_setresuid"



# Which end of the move a parameter is named for. `ruid` is deliberately in neither: ReSukiSU's wrapper
# is resolved by following its forward call, not by reading its names.
OLD_NAMED = re.compile(r"old|previous|prev|before")
NEW_NAMED = re.compile(r"new_uid|new")

BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
LINE_COMMENT = re.compile(r"//[^\n]*")


def without_comments(code: str) -> str:
    """The code with its comments removed.

    The task work's own comment quotes the very call this check reads, and taking that for the call
    would make the check answer with prose.
    """
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", code))


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


def split_arguments(inner: str) -> list[str]:
    """The top-level arguments of a call's argument list."""
    arguments: list[str] = []
    depth = 0
    current = ""
    for character in inner:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        if character == "," and depth == 0:
            arguments.append(current.strip())
            current = ""
        else:
            current += character
    if current.strip():
        arguments.append(current.strip())
    return arguments


def calls_named(code: str, names: tuple[str, ...]) -> list[tuple[str, list[str]]]:
    """Every call to one of `names`, with its arguments split at the top level.

    Depth-counted rather than a regular expression: the wrapper this check has to read forwards
    `ksu_get_uid_t(current_uid())`, which is a call nested inside the argument list.
    """
    pattern = re.compile(r"\b({})\s*\(".format("|".join(names)))
    found: list[tuple[str, list[str]]] = []
    for match in pattern.finditer(code):
        depth = 1
        index = match.end()
        while index < len(code) and depth:
            if code[index] == "(":
                depth += 1
            elif code[index] == ")":
                depth -= 1
            index += 1
        if depth:
            break
        found.append((match.group(1), split_arguments(code[match.end() : index - 1])))
    return found


def parameter_names(parameters: str) -> list[str]:
    """The identifier at the end of each comma-separated parameter."""
    names = []
    for parameter in parameters.split(","):
        tokens = re.findall(r"[A-Za-z_]\w*", parameter)
        if tokens:
            names.append(tokens[-1])
    return names


def declarations(code: str) -> dict[str, list[str]]:
    """The two setuid entry points' parameter lists, as this tree declares them."""
    code = without_comments(code)
    found: dict[str, list[str]] = {}
    for name in (PRIMITIVE, ENTRY):
        match = re.search(r"\b{}\s*\(([^)]*)\)\s*\{{".format(name), code)
        if match:
            found[name] = parameter_names(match.group(1))
    return found


def named_slots(names: list[str]) -> dict[str, int]:
    """{"new": i, "old": j} from a parameter list that names the two ends of the move.

    Only answers when the names do: an unnamed pair (ReSukiSU's `ruid`/`euid`) resolves through the
    forward call instead, and guessing here would be the very assumption this check exists to remove.
    """
    slots: dict[str, int] = {}
    for index, name in enumerate(names[:2]):
        if NEW_NAMED.search(name):
            slots.setdefault("new", index)
        elif OLD_NAMED.search(name):
            slots.setdefault("old", index)
    if "old" in slots and "new" not in slots:
        slots["new"] = 1 - slots["old"]
    return slots


def uid_slots(name: str, table: dict[str, list[str]], code: str) -> dict[str, int]:
    """Which of `name`'s own parameters hold the uid moved *to* and the uid moved *from*.

    A tree where `name` is the engine says so in its parameter names. A tree where it is a wrapper -
    ReSukiSU's is - says so in what it forwards: map the primitive's slots onto the wrapper's
    parameters through that call. "old" is absent when the wrapper reads the previous uid from
    `current_cred()` itself, and then passing one is meaningless rather than wrong.
    """
    names = table.get(name)
    if not names:
        return {}
    if name == PRIMITIVE:
        return named_slots(names)

    body = function_body(code, f"int {name}(")
    forwarded: list[str] | None = None
    if body:
        for called, arguments in calls_named(without_comments(body), (PRIMITIVE,)):
            forwarded = arguments
            break

    primitive = named_slots(table.get(PRIMITIVE, []))
    if forwarded is None or not primitive:
        return named_slots(names)

    slots: dict[str, int] = {}
    for end, position in primitive.items():
        if position < len(forwarded) and forwarded[position] in names:
            slots[end] = names.index(forwarded[position])
    return slots


def setuid_order(manager: str, setuid: str) -> tuple[str, str]:
    """Whether the kretprobe hands the uids over in the order this tree's entry point takes them.

    "ok" or "unchecked" with what was read, "reversed" with the call that has to change.
    """
    body = function_body(manager, TASK_WORK)
    if body is None:
        return "unchecked", "no setresuid kretprobe task work in this tree"

    # The call that forwards the capture is the one that mentions it; anything else the body contains
    # (a comment, a helper) is not the hand-off.
    found = calls_named(without_comments(body), (PRIMITIVE, ENTRY))
    forwarding = [call for call in found if any("work->" in argument for argument in call[1])]
    if not forwarding:
        return "unchecked", "the kretprobe task work calls neither setuid entry point"
    name, arguments = forwarding[0]
    slots = uid_slots(name, declarations(setuid), setuid)
    if not slots:
        return "unchecked", f"this tree does not say which argument of {name}() is which uid"

    expected = {"new": "work->new_uid", "old": "work->old_uid"}
    wrong = [
        end
        for end, position in sorted(slots.items())
        if position < len(arguments) and arguments[position] != expected[end]
    ]
    if wrong:
        return "reversed", (
            f"{name}({', '.join(arguments)}) against {name}() declared as "
            f"({', '.join(declarations(setuid)[name])}), whose argument "
            f"{slots.get('new')} is the uid moved to"
        )
    handed = f"{name}() receives the uid the process moved to as argument {slots['new']}"
    if "old" in slots:
        handed += f" and the one it left as argument {slots['old']}"
    return "ok", f"{handed}, which is where this tree puts them"


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


def patch_hunks(text: str) -> dict[str, list[list[str]]]:
    """{path: [hunk lines]} for every file a diff touches.

    A hunk's lines keep their leading character, so `+` is an addition and a space is context, and the
    diff's own header lines - `index`, `---`, `+++` - are dropped with the text before the first `@@`.
    That is what lets a caller ask what a patch *leaves the file calling* rather than what it mentions:
    the comment this delta puts above the call names the call too.

    The path is normalised to `/`, which is what git writes: a diff that went through a tool which
    rewrote the separators still answers to a lookup by `Path.as_posix()`, rather than reporting the
    file as untouched.
    """
    sections: dict[str, list[list[str]]] = {}
    headers = list(DIFF_SECTION.finditer(text))
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        hunks = [hunk.splitlines() for hunk in HUNK_HEADER.split(text[header.end() : end])[1:]]
        sections.setdefault(header.group(2).replace("\\", "/"), []).extend(hunks)
    return sections


def added_code(hunk: list[str]) -> str:
    """What a hunk adds, with its comments removed.

    Comments out, because the line above the call in this delta explains it and would otherwise be read
    as the call - a check that a comment can satisfy is a check that says nothing.
    """
    return without_comments("\n".join(line[1:] for line in hunk if line.startswith("+")))


def patch_arms_the_spoof(text: str) -> tuple[str, str]:
    """("absent" | "ok" | "missing", detail) for one patch.

    The requirement is read out of the patch rather than asserted: a patch whose tree has no spoof
    feature at all - the tiann leg, whose `on_boot_completed()` arms nothing - is answered `absent` and
    is not a failure, which is the same way `late_load` treats that tree.
    """
    if SPOOF not in text:
        return "absent", f"does not name {SPOOF}, so this tree has no feature to arm"
    for hunk in patch_hunks(text).get(CORE_INIT.as_posix(), []):
        context = without_comments("\n".join(line[1:] for line in hunk if line[:1] in (" ", "+")))
        if LATE_BRANCH_CUE in context and re.search(rf"\b{LATE_ARM}\s*\(", added_code(hunk)):
            return "ok", f"arms the spoof beside `{LATE_BRANCH_CUE}` in {CORE_INIT.as_posix()}"
    if LATE_ARM not in text:
        return "missing", f"never leaves {CORE_INIT.as_posix()} calling {LATE_ARM}()"
    return "missing", (
        f"names {LATE_ARM}() but not in a hunk that sits beside `{LATE_BRANCH_CUE}`, "
        "so it is not on the late-load path"
    )


def check_patches(directory: Path) -> int:
    """Every published patch that carries the spoof has to arm it on the late-load path.

    No checkout and no network: the diffs are the whole input, which is what makes this able to cover a
    flavour and ref that no run of the build workflow ever asks for.
    """
    files = sorted(directory.glob("*.patch"))
    if not files:
        print(f"error: no patch under {directory}", file=sys.stderr)
        return 2

    status = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="surrogateescape")
        except OSError as error:
            print(f"error: cannot read {error.filename}: {error}", file=sys.stderr)
            return 2
        state, detail = patch_arms_the_spoof(text)
        if state != "missing":
            print(f"{path}: {detail}")
            continue
        print(f"error: {path} {detail}")
        print("       A late-loaded module never runs `on_boot_completed()`, which is where upstream arms")
        print("       this hook, so the feature is registered and its switch reads enabled while the")
        print("       kprobe that suppresses the denial is never registered. That is the delta's own")
        print("       late load - the path that reaches a locked device - so it is every device's case.")
        status = 1
    return status


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

    # The setuid hand-off, in both conventions the trees declare - and the comment quotes the call
    # itself, so the fixtures also hold the check to reading code rather than prose.
    def task_work(call: str) -> str:
        return f"""{TASK_WORK}
{{
    struct ksu_setresuid_task_work *work = container_of(callback, struct ksu_setresuid_task_work, callback);

    // {ENTRY}(work->new_uid, work->old_uid) would be this tree's other order
    {call};
    kfree(work);
}}
"""

    engine = f"""int {PRIMITIVE}(uid_t new_uid, uid_t old_uid)
{{
    pr_info("handle_setresuid from %d to %d\\n", old_uid, new_uid);
    return 0;
}}

int {ENTRY}(uid_t old_uid, uid_t new_uid)
{{
    pr_info("handle_setresuid from %d to %d\\n", old_uid, new_uid);
    return 0;
}}
"""

    # ReSukiSU's shape: the entry point we call is a wrapper whose `ruid` is the uid moved *to*.
    wrapper = f"""int {PRIMITIVE}(uid_t new_uid, uid_t old_uid)
{{
    return 0;
}}

int {ENTRY}(uid_t ruid, uid_t euid, uid_t suid)
{{
    return {PRIMITIVE}(ruid, ksu_get_uid_t(current_uid()));
}}
"""

    # The patch reader, which is what covers a patch no run applies. Four shapes: the arming in the
    # late-load hunk, the call only in the comment above it, the call added in another hunk of the
    # same file, and a tree with no feature to arm at all.
    core = CORE_INIT.as_posix()

    def patch(hunks: str) -> str:
        return (
            f"diff --git a/{core} b/{core}\n"
            f"index 1111111..2222222 100644\n"
            f"--- a/{core}\n"
            f"+++ b/{core}\n"
            f"{hunks}"
        )

    armed = (
        "@@ -166,6 +189,10 @@ int __init kernelsu_init(void)\n"
        " \t\tksu_file_wrapper_init();\n"
        " \n"
        " \t\tksu_boot_completed = true;\n"
        "+\n"
        "+// What on_boot_completed() does for a module that was present at boot. A late-loaded\n"
        "+// one never gets the event, so the phone being past boot here is the cue to arm it.\n"
        f"+\t\t{LATE_ARM}();\n"
        " \t\ttrack_throne(false);\n"
        " \n"
        " \t\tif (!getenforce()) {\n"
    )

    # The cue in one hunk and the call in another: named by the file, on no path at all.
    apart = (
        "@@ -166,6 +189,7 @@ int __init kernelsu_init(void)\n"
        " \t\tksu_boot_completed = true;\n"
        "+\t\ttrack_throne(false);\n"
        "@@ -400,6 +424,7 @@ static void ksu_something_else(void)\n"
        f"+\t{LATE_ARM}();\n"
    )

    engine_ok = setuid_order(task_work(f"{ENTRY}(work->old_uid, work->new_uid)"), engine)[0] == "ok"
    engine_caught = setuid_order(task_work(f"{ENTRY}(work->new_uid, work->old_uid)"), engine)[0] == "reversed"
    primitive_ok = setuid_order(task_work(f"{PRIMITIVE}(work->new_uid, work->old_uid)"), engine)[0] == "ok"
    wrapper_ok = setuid_order(task_work(f"{PRIMITIVE}(work->new_uid, work->old_uid)"), wrapper)[0] == "ok"
    wrapper_caught = setuid_order(task_work(f"{ENTRY}(work->old_uid, work->new_uid)"), wrapper)[0] == "reversed"
    undeclared_ok = setuid_order(task_work(f"{ENTRY}(work->old_uid, work->new_uid)"), "")[0] == "unchecked"

    results = [
        ("complete branch", good),
        ("missing registration", caught),
        ("no branch at all", absent_ok),
        ("late load arms", late_ok),
        ("late load missing the arming", late_caught),
        ("event that arms nothing", late_absent_ok),
        ("skipped work is reported", reported),
        ("setuid order read from the tree", engine_ok),
        ("setuid order reversed on that tree", engine_caught),
        ("setuid order through the primitive", primitive_ok),
        ("setuid order through a wrapper", wrapper_ok),
        ("setuid order reversed on a wrapper tree", wrapper_caught),
        ("tree that does not declare it", undeclared_ok),
        ("patch arms the spoof", patch_arms_the_spoof(patch(armed))[0] == "ok"),
        (
            # The sharp one: the call is written out in full, so a check that looks for the name in
            # the patch text passes, and only one that strips comments and looks for a call does not.
            "patch names it only inside a comment",
            patch_arms_the_spoof(
                patch(armed.replace(f"+\t\t{LATE_ARM}();\n", f"+\t\t// {LATE_ARM}();\n"))
            )[0]
            == "missing",
        ),
        ("patch adds it off the late path", patch_arms_the_spoof(patch(apart))[0] == "missing"),
        (
            "patch for a tree with no spoof",
            patch_arms_the_spoof(patch("@@ -1 +1 @@\n+// nothing to arm\n"))[0] == "absent",
        ),
    ]
    for label, passed in results:
        print(f"self-test: {label} {'ok' if passed else 'FAILED'}")
    if all(passed for _, passed in results):
        print("self-test: every check can fail, and none fails on a correct tree or a correct patch")
        return 0
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tree", help="checkout the patch has been applied to")
    parser.add_argument(
        "--patches",
        metavar="DIR",
        nargs="?",
        const=PATCHES.as_posix(),
        help=f"patches to check where no checkout exists (default: {PATCHES.as_posix()})",
    )
    parser.add_argument("--self-test", action="store_true", help="run the checks against fixtures")
    arguments = parser.parse_args()

    if arguments.self_test:
        return self_test()
    if arguments.patches:
        return check_patches(Path(arguments.patches))
    if not arguments.tree:
        parser.error("one of --tree, --patches or --self-test is required")

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

    setuid = read(root / SETUID)
    if setuid is None:
        print(f"{root / SETUID}: unreadable, so the setuid hand-off was not checked")
    else:
        state, detail = setuid_order(text, setuid)
        if state == "reversed":
            print(f"error: {root / FILE} hands over the setuid kretprobe's capture in the wrong order:")
            print(f"         {detail}")
            print("       Every app spawn then arrives as a move to uid 0, so ksu_is_manager_uid() never")
            print("       matches, the manager branch of ksu_handle_setuid() never runs, and no manager")
            print("       is handed the driver fd - while root, which arrives through execve, keeps")
            print("       working. The manager app reads that as \"not installed\" on a loaded kernel.")
            status = 1
        else:
            print(f"{root / SETUID}: {detail}")
    return status


if __name__ == "__main__":
    sys.exit(main())
