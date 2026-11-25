# filesync.py -- tests and checks
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

# Expected SHA-256 digests of the extracted rclone binaries for the default set.
SHA256_v1.66.0_linux-amd64 := 8cb7a66c028a81f5ed67460d0ae7bb9996ff439451514c8586463eaa905d6f82
SHA256_v1.76.0_linux-amd64 := 62e4d3e5432f2c685f1834b8bb342058ff0611c99d048aeb44bb8d8fd611527e

# shared tool environment on the development box (ruff), if present
SHARED_ENV := $(wildcard /mnt/shared/.pylibs/env.sh)
WITH_ENV   := $(if $(SHARED_ENV),. $(SHARED_ENV) &&,)

.PHONY: test lint fetch-rclone clean

test: $(RCLONE_BINS)
	FILESYNC_TEST_RCLONE="$(abspath $(RCLONE_BINS))" \
	FILESYNC_TEST_SERVER="$(abspath $(firstword $(RCLONE_BINS)))" PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
	  $(PYTHON) -m pytest -q tests

lint:
	$(WITH_ENV) ruff check --line-length 100 --target-version py311 filesync.py tests

fetch-rclone: $(RCLONE_BINS)

$(RCLONE_DIR)/rclone-%:
	mkdir -p $(RCLONE_DIR)
	curl -fsSL -o $@.zip https://downloads.rclone.org/$*/rclone-$*-$(RCLONE_ARCH).zip
	$(PYTHON) -c "import hashlib, pathlib, sys, zipfile; \
	  z = zipfile.ZipFile(sys.argv[1]); \
	  n = next(x for x in z.namelist() if x.endswith('/rclone')); \
	  data = z.read(n); \
	  want = sys.argv[3]; \
	  got = hashlib.sha256(data).hexdigest(); \
	  assert not want or got == want, f'SHA-256 mismatch: {got} != {want}'; \
	  pathlib.Path(sys.argv[2]).write_bytes(data)" $@.zip $@.tmp "$(SHA256_$*_$(RCLONE_ARCH))"
	chmod +x $@.tmp
	mv $@.tmp $@
	rm -f $@.zip

clean:
	rm -rf build
