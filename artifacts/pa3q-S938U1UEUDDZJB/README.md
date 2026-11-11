# S938U1UEUDDZJB CVE-2026-98260 payload

This is the DDZJB timer payload. It carries the SamTimer chain that was
verified on `S938U1UEUDDZJB`: timer UAF -> pipe read/write -> fake
`system_unbound_wq` work -> `call_usermodehelper` -> `uhexec` -> KSud
late-load.

- `cve-2026-98260-app.so`: the app payload. Its constructor stages the
  embedded standalone timer binary and `uhexec`, writes the late-load action
  for the manager package named by `CVE98260_KSU_PACKAGE`, then waits for the
  `kernelsu` module and prints the success markers the app reads.
- The timer process parks after the UMH handoff, by design: it holds the freed
  page and its zombie triggers. It must not be killed. A second attempt in the
  same boot is refused, and a reboot clears it.

The three feed rows point at the exact DDZJB `ksud` daemons built by the
KernelSU workflow, each embedding its own DDZJB kernel module. The app stages
the selected flavour's daemon at `/data/local/tmp/ksud-s25u-kdp` before the
payload starts.
