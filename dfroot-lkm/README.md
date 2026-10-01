# dfroot-lkm

The kernel module the universal root's chain loads, built one per KMI.

## Where it comes from

[DFRoot](https://github.com/diabl0w/DFRoot) at `e47ea6e` — the commit that removed the libc patch from
that chain and moved the privileged half into this module — updated to `23085ef` ("Fix DEFEX hook on some
devices"). The chain's shellcode `insmod`s it and does nothing else; the module forces SELinux permissive,
defeats defex with two kprobes, runs the daemon's `late-load` from kernel context with
`call_usermodehelper`, writes `/dev/dfm0` on success or `/dev/dfm1` on failure, and returns `-E2BIG` so it
unloads itself. There is no `module_exit` and no unload path.

`23085ef` changed *which* functions are hooked. The pair was `task_defex_user_exec` and
`get_dc_target_dpath`; it is now `task_defex_user_exec` and **`task_defex_enforce`**. The path-based hook
was the DEFEX entry point on the kernels it was written against and is not on others, where enforcement
runs through `task_defex_enforce` — so on those devices the hook registered cleanly, was never called,
and the `insmod` was refused with nothing in the log to explain it. Both hooks are now registered and
neither is required: a kernel that names only one of the two still loads, and a kernel that names neither
says so in dmesg instead of failing silently.

## What differs from that revision

Three things, and nothing else:

1. **The daemon path.** Upstream names their own package (`/data/user_de/0/df.root/ksud`); this names
   `/data/user_de/0/dev.rushiranpise.rmgnext/ksud`, which is where the app stages the daemon it downloaded
   — the same path the chain's shellcode read before this change.
2. **`package_name` is a module parameter**, not a compiled-in literal. One chain serves KernelSU,
   KernelSU-Next and ReSukiSU, and the manager the daemon is told to serve is what the app chose for this
   run; a literal here would crown upstream's manager for all three.
3. **`--ro-partitions` and `--soft-reboot` are not passed.** Both are options of upstream's KernelSU fork
   rather than of the daemons built here, and both behaviours already exist in the app: the read-only
   partition wall, and *Auto soft restart*. Passing them would make the daemon refuse its own command line.

And one difference in *kind*, which is about ordering rather than about what is passed. Upstream registers
both DEFEX probes first and looks the usermode-helper symbols up after, so a device where
`call_usermodehelper_setup`/`_exec` are not exported returns `-EINVAL` from init with two probes still
registered — and an init that returns an error unloads the module, leaving probes that point into memory
which is no longer there. Here the probes are registered immediately before the daemon's exec and removed
immediately after it, so they exist for exactly the window the daemon's transition needs them and a
missing helper symbol leaves nothing behind.

## Building it

The workflow is `.github/workflows/dfroot-lkm.yml`: a matrix over the eight KMIs, each inside
`ghcr.io/ylarod/ddk-min:<kmi>-<release>`, then the same size diet upstream applies — `-Os`, unwind tables
dropped, `llvm-objcopy --strip-unneeded` and the `-R` removals — because the module is written through the
exploit page by page and its size is a page count.

The artifacts are `dirtyfrag-<kmi>.ko`, and they belong in the app's
`app/src/main/cpp/dfroot/ko/`, which is where the chain embeds them from (`.incbin`, see that directory's
`CMakeLists.txt`). Nothing here is device-specific: one build covers every device whose kernel belongs to
that KMI.

The workflow also checks what a rebase could silently undo — that the built module still names **this**
app's daemon path and carries **neither** of the two fork-only flags — because all three of those live in
strings that a merge resolves without a conflict.
