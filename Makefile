# webde_sync.py -- tests and checks
#
#   make test          unit + integration tests against every rclone in RCLONE_VERSIONS
#   make lint          ruff
#   make fetch-rclone  download the test rclone binaries into build/rclone/
#   make clean         remove build/

PYTHON          ?= python3
RCLONE_VERSIONS ?= v1.66.0 v1.76.0
RCLONE_ARCH     ?= linux-amd64
RCLONE_DIR      := build/rclone
RCLONE_BINS     := $(addprefix $(RCLONE_DIR)/rclone-,$(RCLONE_VERSIONS))

# shared tool environment on the development box (ruff), if present
SHARED_ENV := $(wildcard /mnt/shared/.pylibs/env.sh)
WITH_ENV   := $(if $(SHARED_ENV),. $(SHARED_ENV) &&,)

.PHONY: test lint fetch-rclone clean

test: $(RCLONE_BINS)
	WEBDE_SYNC_TEST_RCLONE="$(abspath $(RCLONE_BINS))" \
	WEBDE_SYNC_TEST_SERVER="$(abspath $(firstword $(RCLONE_BINS)))" PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
	  $(PYTHON) -m pytest -q tests

lint:
	$(WITH_ENV) ruff check --line-length 100 --target-version py311 webde_sync.py tests

fetch-rclone: $(RCLONE_BINS)

$(RCLONE_DIR)/rclone-%:
	mkdir -p $(RCLONE_DIR)
	curl -fsSL -o $@.zip https://downloads.rclone.org/$*/rclone-$*-$(RCLONE_ARCH).zip
	$(PYTHON) -c "import sys, zipfile; z = zipfile.ZipFile(sys.argv[1]); \
	  n = next(x for x in z.namelist() if x.endswith('/rclone')); \
	  open(sys.argv[2], 'wb').write(z.read(n))" $@.zip $@
	chmod +x $@
	rm $@.zip

clean:
	rm -rf build
