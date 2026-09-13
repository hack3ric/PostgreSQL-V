EXTENSION = vector
EXTVERSION = 0.8.0

MODULE_big = vector
DATA = $(wildcard sql/*--*--*.sql)
DATA_built = sql/$(EXTENSION)--$(EXTVERSION).sql
OBJS = src/bitutils.o src/bitvec.o src/halfutils.o src/halfvec.o src/hnsw.o src/hnswbuild.o src/hnswinsert.o src/hnswscan.o src/hnswutils.o src/hnswvacuum.o src/ivfbuild.o src/ivfflat.o src/ivfinsert.o src/ivfkmeans.o src/ivfscan.o src/ivfutils.o src/ivfvacuum.o src/sparsevec.o src/vector.o \
	   src/lsmindex.o src/lsm_segment.o src/lsmbackground.o src/lsmindex_io.o src/ringbuffer.o src/tasksend.o src/vector_index_worker.o src/index_recovery_worker.o src/vectorindeximpl.o src/utils.o src/folly_f14_link_alias.o src/statuspage.o \
	   src/replication_rmgr.o src/standby_memtable.o src/replication_gucs.o src/pending_fetch_queue.o src/replication_server.o src/segment_adoption.o src/segment_fetcher.o src/segment_vacuum_redo.o
HEADERS = src/halfvec.h src/sparsevec.h src/vector.h

TESTS = $(wildcard test/sql/*.sql)
REGRESS = $(patsubst test/sql/%.sql,%,$(TESTS))
REGRESS_OPTS = --inputdir=test --load-extension=$(EXTENSION)

# To compile for portability, run: make OPTFLAGS=""
OPTFLAGS = -march=native

# Mac ARM doesn't always support -march=native
ifeq ($(shell uname -s), Darwin)
	ifeq ($(shell uname -p), arm)
		# no difference with -march=armv8.5-a
		OPTFLAGS =
	endif
endif

# PowerPC doesn't support -march=native
ifneq ($(filter ppc64%, $(shell uname -m)), )
	OPTFLAGS =
endif

# For auto-vectorization:
# - GCC (needs -ftree-vectorize OR -O3) - https://gcc.gnu.org/projects/tree-ssa/vectorization.html
# - Clang (could use pragma instead) - https://llvm.org/docs/Vectorizers.html
PG_CFLAGS += $(OPTFLAGS) -ftree-vectorize -fassociative-math -fno-signed-zeros -fno-trapping-math

# Debug GCC auto-vectorization
# PG_CFLAGS += -fopt-info-vec

# Debug Clang auto-vectorization
# PG_CFLAGS += -Rpass=loop-vectorize -Rpass-analysis=loop-vectorize

CXX = g++

# The runtime builder writes this file from its pinned Conan graph. It carries
# only compiler and linker flags; no developer home directory is baked into
# this Makefile or into vector.so's RPATH.
POSTGRESQLV_DEPS_MK ?= $(CURDIR)/postgresqlv-deps.mk
-include $(POSTGRESQLV_DEPS_MK)

PG_CONFIG ?= /opt/postgresql17/bin/pg_config
KNOWHERE_ROOT ?= /opt/postgresqlv-runtime/knowhere-install
KNOWHERE_INCLUDEDIR ?= $(KNOWHERE_ROOT)/include
KNOWHERE_LIBDIR ?= $(KNOWHERE_ROOT)/lib
KNOWHERE_SOURCE ?= /opt/postgresqlv-runtime/src/knowhere
POSTGRESQLV_RUNTIME_LIBDIR ?= $(shell $(PG_CONFIG) --pkglibdir)/postgresqlv-runtime

PGV_DEP_CPPFLAGS ?=
PGV_DEP_SHLIB_LINK ?=

CXXFLAGS += -std=c++20 $(OPTFLAGS) -O3 -Ofast -march=native -fopenmp \
			-ftree-vectorize -fassociative-math -fno-signed-zeros -fno-trapping-math

PG_CPPFLAGS += -I$(shell $(PG_CONFIG) --includedir-server)
PG_CPPFLAGS += -I$(shell $(PG_CONFIG) --includedir)/internal
PG_CPPFLAGS += -I$(KNOWHERE_INCLUDEDIR) -I$(KNOWHERE_SOURCE)/src \
              -I$(KNOWHERE_SOURCE)/thirdparty \
              -I$(KNOWHERE_SOURCE)/thirdparty/faiss -I/usr/include/openblas
PG_CPPFLAGS += $(PGV_DEP_CPPFLAGS)
PG_CPPFLAGS += -DGLOG_NO_ABBREVIATED_SEVERITIES
SHLIB_LINK += -L$(KNOWHERE_LIBDIR) -lknowhere -lknowhere_utils -lfaiss \
              $(PGV_DEP_SHLIB_LINK) -lpthread -lstdc++ -lc -lopenblas -fopenmp \
              -Wl,-rpath,'$$ORIGIN/postgresqlv-runtime'


%.o: %.cpp
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -fPIC -c $< -o $@

all: sql/$(EXTENSION)--$(EXTVERSION).sql

sql/$(EXTENSION)--$(EXTVERSION).sql: sql/$(EXTENSION).sql
	cp $< $@

PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)

# PGXS's LLVM rule has a separate C++ flag set and otherwise defaults to the
# PostgreSQL build's C++17 mode. Knowhere's pinned Folly requires C++20.
override BITCODE_CXXFLAGS += -std=c++20

# for Mac
ifeq ($(PROVE),)
	PROVE = prove
endif

# for Postgres < 15
PROVE_FLAGS += -I ./test/perl

prove_installcheck:
	rm -rf $(CURDIR)/tmp_check
	cd $(srcdir) && TESTDIR='$(CURDIR)' PATH="$(bindir):$$PATH" PGPORT='6$(DEF_PGPORT)' PG_REGRESS='$(top_builddir)/src/test/regress/pg_regress' $(PROVE) $(PG_PROVE_FLAGS) $(PROVE_FLAGS) $(if $(PROVE_TESTS),$(PROVE_TESTS),test/t/*.pl)

.PHONY: dist

dist:
	mkdir -p dist
	git archive --format zip --prefix=$(EXTENSION)-$(EXTVERSION)/ --output dist/$(EXTENSION)-$(EXTVERSION).zip master

# for Docker
PG_MAJOR ?= 17

.PHONY: docker

docker:
	docker build --pull --no-cache --build-arg PG_MAJOR=$(PG_MAJOR) -t pgvector/pgvector:pg$(PG_MAJOR) -t pgvector/pgvector:$(EXTVERSION)-pg$(PG_MAJOR) .

.PHONY: docker-release

docker-release:
	docker buildx build --push --pull --no-cache --platform linux/amd64,linux/arm64 --build-arg PG_MAJOR=$(PG_MAJOR) --build-arg DEBIAN_CODENAME=bookworm -t pgvector/pgvector:pg$(PG_MAJOR) -t pgvector/pgvector:pg$(PG_MAJOR)-bookworm -t pgvector/pgvector:$(EXTVERSION)-pg$(PG_MAJOR) -t pgvector/pgvector:$(EXTVERSION)-pg$(PG_MAJOR)-bookworm .

.PHONY: docker-release-trixie

docker-release-trixie:
	docker buildx build --push --pull --no-cache --platform linux/amd64,linux/arm64 --build-arg PG_MAJOR=$(PG_MAJOR) --build-arg DEBIAN_CODENAME=trixie -t pgvector/pgvector:pg$(PG_MAJOR)-trixie -t pgvector/pgvector:$(EXTVERSION)-pg$(PG_MAJOR)-trixie .
