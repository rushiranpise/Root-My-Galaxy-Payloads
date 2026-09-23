# SPDX-License-Identifier: GPL-2.0
#
# Two builds share this directory, and they must not see each other's variables.
#
#   make -C $KDIR M=$PWD modules RMG_ENFORCING_OFFSET=<n>   the kernel module
#   make test                                              the layout rule, host compiler
#
# Kbuild includes this file a second time with KERNELRELEASE set, so everything that is ours lives
# behind that test: an `all:` target of our own would otherwise become the module build's default.
#
# RMG_ENFORCING_OFFSET is the byte offset of `selinux_state.enforcing`, derived at build time from
# the kernel tree being built against. -1 (the default) means nobody derived it, and the module then
# refuses to write unless a caller passes `offset=` at load.

obj-m += rmg_permissive.o
ccflags-y += -DRMG_ENFORCING_OFFSET=$(RMG_ENFORCING_OFFSET)

ifeq ($(KERNELRELEASE),)

KDIR ?=
RMG_ENFORCING_OFFSET ?= -1
HOSTCC ?= cc

.PHONY: all modules test clean

all: modules

modules:
	@test -n "$(KDIR)" || { echo "set KDIR to a prepared kernel tree (the DDK image exports it)"; exit 1; }
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

test: tests/layout_test
	./tests/layout_test

tests/layout_test: tests/layout_test.c selinux_layout.h
	$(HOSTCC) -std=c11 -Wall -Wextra -Werror -O2 -o $@ tests/layout_test.c

clean:
	@if [ -n "$(KDIR)" ]; then $(MAKE) -C $(KDIR) M=$(CURDIR) clean; fi
	$(RM) tests/layout_test *.o *.ko *.mod *.mod.c *.symvers *.order .*.cmd

endif
