# PostgreSQL-V Integration Plan

## Status and decisions

This document defines the work required to add PostgreSQL-V 2.0 as a dynamic
ANN baseline alongside FreshDiskANN and OdinANN.

Status: runnable smoke integration complete. The architectural and
benchmark-policy decisions below remain the working contract. Changes to the
public semantics, artifact ownership, durability profile, or benchmark
interpretation must be written back into this document with the corresponding
code and tests.

The current worktree contains the adapter, PostgreSQL-V source patches, a
PostgreSQL-V-only CMake configuration, a build/load/restart test, and a
four-client concurrent data-plane test. PostgreSQL 17.10 from the AUR package
is installed under `/opt/postgresql17`; the helper has built the pinned
Knowhere runtime and installed a dependency-closed `vector.so` there. On
2026-09-13, both server-backed CTests and both benchmark frontends completed on
a 256-vector, 32-dimensional smoke workload. These runs prove integration
correctness at small scale only; they are not publication-scale performance
results.

The first supported configuration will be:

- PostgreSQL-V HNSW
- `float` vectors and `uint32_t` tags
- L2 and cosine distance
- concurrent search, insert, and remove operations over libpq
- PostgreSQL-V's native automatic background flush, merge, and rebuild work
- a self-contained index artifact rooted at one `index_directory`
- PostgreSQL 17 installed from the Arch User Repository package
  [`postgresql17`](https://aur.archlinux.org/packages/postgresql17)

PostgreSQL-V's DiskANN and IVFFlat implementations, replication benchmarking,
and non-float scalar types are explicitly out of scope for the first
integration. They can be added after the HNSW path is correct and repeatable.

The adapter will wrap SQL and the PostgreSQL client/server boundary directly.
It will not attempt to make PostgreSQL-V behave like an embedded C++ library.
The existing synchronous `ANNIndex` methods remain the benchmark-facing
contract, while an internal libpq connection pool provides actual concurrency.

The source audit in this plan refers to PostgreSQL-V commit
[`0d23798b1cc822dfadfda552cd0cd62ae49978be`](https://github.com/purduedb/PostgreSQL-V/tree/0d23798b1cc822dfadfda552cd0cd62ae49978be).
The paper describing PostgreSQL-V 2.0 is
[Building An Integrated Vector Database System in PostgreSQL](https://arxiv.org/abs/2608.15994).

## Goals

The completed integration must provide the same high-level workflow as the
existing baselines:

```cpp
PostgreSQLVIndex<float>::build(data_path, tags_path, index_directory,
                               build_options);

PostgreSQLVIndex<float> index(index_directory, load_options);
index.search(query, k, tags, distances);
index.insert(point, tag);
index.remove(tag);
index.merge(merge_threads);
```

All structural tuning belongs in `BuildOptions`. All runtime, search, client
pool, and PostgreSQL-V background-maintenance tuning belongs in `LoadOptions`.
Individual data operations should accept only data and logical operation
parameters such as `k`; they should not accept PostgreSQL or ANN tuning
parameters. The existing baseline-specific `merge(thread_count)` API is an
intentional exception and will not be redesigned as part of this integration.

The implementation must also:

1. Build and load an index from a single artifact path.
2. Preserve arbitrary input tags rather than assuming identity tags.
3. Allow one index object to serve several benchmark threads concurrently.
4. Persist inserted and removed records across a clean server restart.
5. Make all effective PostgreSQL-V and PostgreSQL settings visible in the
   manifest or benchmark output.
6. Avoid requiring FreshDiskANN or OdinANN dependencies for a PostgreSQL-V-only
   build.
7. Make worker counts independent of host CPU discovery and prevent nested
   Knowhere, Faiss/OpenMP, or BLAS parallelism from silently multiplying them.

## Non-goals for the first milestone

- Running PostgreSQL-V and pgvector in the same PostgreSQL installation. Both
  install an extension named `vector`, so PostgreSQL-V will use an isolated
  PostgreSQL 17 prefix.
- Treating PostgreSQL-V background compaction as an explicit FreshDiskANN-style
  final merge.
- Batching individual benchmark inserts or deletes into larger SQL
  transactions. Bulk transfer is permitted only during initial index build.
- Hiding durability changes. The default profile will retain PostgreSQL WAL,
  `fsync`, and `synchronous_commit` behavior.
- Supporting a remote, externally managed database through the path-based
  constructor. Such a mode can be added later through a separate factory.

## Current source constraints

The adapter design must account for the following properties of the audited
PostgreSQL-V source.

### Build portability

The extension Makefile currently contains absolute developer paths for
PostgreSQL, Knowhere, Faiss, Folly, glog, and nlohmann-json. It also embeds
absolute runtime search paths. See the
[PostgreSQL-V Makefile](https://github.com/purduedb/PostgreSQL-V/blob/0d23798b1cc822dfadfda552cd0cd62ae49978be/postgresqlv/Makefile#L43-L87).
The PostgreSQL-V fork must therefore be patched to accept normal build
variables and must pin its native dependencies.

### Fixed request limits

The shared request structure currently caps vectors at 1024 dimensions and
search output at 1000 candidates. See
[`ringbuffer.h`](https://github.com/purduedb/PostgreSQL-V/blob/0d23798b1cc822dfadfda552cd0cd62ae49978be/postgresqlv/src/ringbuffer.h#L27-L29).
The adapter must validate these limits before sending work to the server.

### Hard-coded search candidate count

The HNSW index scan currently calls the decoupled index with `top_k = 150`
regardless of SQL `LIMIT`. See
[`hnswscan.c`](https://github.com/purduedb/PostgreSQL-V/blob/0d23798b1cc822dfadfda552cd0cd62ae49978be/postgresqlv/src/hnswscan.c#L154-L159).
A session GUC is required to make this an honest `LoadOptions` setting.

### Hard-coded maintenance parameters

Memtable capacity, the number of memtables, segment capacity, index slots, and
cold-start behavior are compile-time constants. See
[`lsmindex.h`](https://github.com/purduedb/PostgreSQL-V/blob/0d23798b1cc822dfadfda552cd0cd62ae49978be/postgresqlv/src/lsmindex.h#L22-L54).
Initial `CREATE INDEX` HNSW options also do not consistently control HNSW
segments built later by flush and merge workers.

### Shared-memory requirement

The upstream literal 4,000,000,000-byte add-in request has been replaced with
the actual LSM static structures, an 8 MiB allocator/index margin, and the full
ring-buffer task/result pools. The largest `MemtableBuffer` portion is
1,077,392,384 bytes (about 1.00 GiB) before those additions. Its C structures
still reserve the maximum memtable layout, so `memtable_capacity` controls
usable entries rather than shrinking that reservation; making allocation fully
capacity-dependent is a separate storage-layout change. Use at least 2 GiB of
`/dev/shm` for a private server and retain the 8 GiB project runtime setting.

### Hardware-derived and nested native parallelism

PostgreSQL-V currently creates its outer segment-search executor with
`std::thread::hardware_concurrency() / 2`, while its maintenance and merge
pthread pools are fixed at four and two threads. See the
[`GetPgOuterSearchExecutor` implementation](https://github.com/purduedb/PostgreSQL-V/blob/0d23798b1cc822dfadfda552cd0cd62ae49978be/postgresqlv/src/vectorindeximpl.cpp#L1576-L1652)
and the
[`vector_index_worker` pool definitions](https://github.com/purduedb/PostgreSQL-V/blob/0d23798b1cc822dfadfda552cd0cd62ae49978be/postgresqlv/src/vector_index_worker.c#L109-L174).
This is not reproducible under CPU affinity: on Linux,
`std::thread::hardware_concurrency()` can continue to report all online CPUs
even when `taskset` restricts the process to a smaller mask.

There are additional concurrency layers below those three PostgreSQL-V pools.
PostgreSQL-V calls Knowhere as its ANN factory and serialization/search API;
the explicit `INDEX_FAISS_IVFFLAT` and `INDEX_FAISS_IDMAP` implementations and
some direct distance routines use Faiss. The extension is also built and linked
with OpenMP and BLAS. See
[`vectorindeximpl.cpp`](https://github.com/purduedb/PostgreSQL-V/blob/0d23798b1cc822dfadfda552cd0cd62ae49978be/postgresqlv/src/vectorindeximpl.cpp#L255-L557)
and the
[`PostgreSQL-V Makefile`](https://github.com/purduedb/PostgreSQL-V/blob/0d23798b1cc822dfadfda552cd0cd62ae49978be/postgresqlv/Makefile#L43-L74).
Knowhere owns global build and search pools, while Faiss CPU operations may use
OpenMP and a threaded BLAS. These layers must be configured explicitly; CPU
affinity controls where their threads may run, not necessarily how many they
create.

## Target architecture

### Artifact ownership

Each built index will own a private PostgreSQL cluster:

```text
<index_directory>/
  manifest.json
  pgdata/
  segments/
  logs/
```

The PostgreSQL installation is not copied into every artifact. It is a shared,
versioned runtime under `/opt/postgresql17`. The manifest records the actual
PostgreSQL version and PostgreSQL-V commit so incompatible artifacts fail early.

Runtime-only files live outside the artifact, under a short path to avoid Unix
socket path-length limits:

```text
/tmp/dann-pgv-<hash>/
  .s.PGSQL.<port>
  adapter.lock
```

The adapter acquires an exclusive filesystem lock before starting a server for
an artifact. Two adapter processes must never start two postmasters over the
same `pgdata` directory.

### Process lifecycle

`build()` will start a temporary postmaster, load the source data, create the
index, checkpoint, and stop the postmaster. The load constructor will start the
artifact's postmaster and establish the connection pool. The destructor will
drain connections, checkpoint, and stop the server without throwing.

Subprocesses must be launched with argument arrays rather than interpolated
shell commands. PostgreSQL must run as an unprivileged operating-system user.
Tests executed by a root CI container should use a dedicated benchmark user and
give that user ownership of temporary artifact directories.

### Connection model

One `PGconn` cannot serve multiple threads simultaneously. The adapter will own
a bounded connection pool and lease one exclusive connection per operation.
The pool size should normally equal or exceed the maximum number of concurrent
benchmark workers.

Each connection is initialized once with its session settings and prepared SQL
statements. A failed operation must roll back its transaction if necessary and
return a clean connection to the pool, or discard and replace a broken
connection.

## Public adapter API

Add `include/baselines/postgresqlv.hpp` with the following conceptual surface:

```cpp
namespace dynamic_ann_benchmark::postgresqlv {

enum class IndexKind { Hnsw };

struct BuildOptions {
  DistanceMetric metric = DistanceMetric::L2;
  IndexKind index_kind = IndexKind::Hnsw;
  uint32_t graph_degree = 16;
  uint32_t build_list_size = 40;
  uint32_t build_threads = 1;
  uint32_t maintenance_work_mem_mb = 1024;
  uint32_t memtable_capacity = 50000;
};

struct LoadOptions {
  uint32_t search_list_size = 150;
  uint32_t search_candidates = 150;
  uint32_t connection_pool_size = 1;
  uint32_t search_worker_threads = 1;
  uint32_t maintenance_worker_threads = 4;
  uint32_t background_merge_threads = 2;
  double deletion_rebuild_ratio = 0.30;
  uint32_t statement_timeout_ms = 0;
  bool synchronous_commit = true;
  bool force_index_scan = true;
  // Disabled by default: PostgreSQL-V v2.0's mmap-to-memory upgrade races
  // concurrent search. Enable only when explicitly evaluating that path.
  bool mmap_cold_start = false;
};

template<typename T, typename TagT = uint32_t>
class PostgreSQLVIndex final : public ANNIndex<T, TagT> {
public:
  using BuildOptions = postgresqlv::BuildOptions;
  using LoadOptions = postgresqlv::LoadOptions;

  static void build(const std::string &data_path,
                    const std::string &initial_tags_path,
                    const std::string &index_directory,
                    const BuildOptions &options);

  explicit PostgreSQLVIndex(const std::string &index_directory,
                            const LoadOptions &options);
  ~PostgreSQLVIndex() override;

  uint64_t dimensions() const noexcept override;
  void search(std::span<const T> query, uint64_t k,
              std::span<TagT> result_tags,
              std::span<float> result_distances) override;
  void insert(std::span<const T> point, const TagT &tag) override;
  void remove(const TagT &tag) override;
  void merge(std::size_t thread_count) override;
};

} // namespace dynamic_ann_benchmark::postgresqlv
```

Only `PostgreSQLVIndex<float, uint32_t>` will be explicitly instantiated for the
first milestone. Unsupported instantiations should fail at compile time, and
unsupported manifest scalar types should fail at load time.

The three worker settings have deliberately narrow meanings:

- `search_worker_threads` is the size of PostgreSQL-V's outer Folly executor
  that runs per-segment search tasks.
- `maintenance_worker_threads` is the size of PostgreSQL-V's flush/rebuild
  pthread pool.
- `background_merge_threads` is the size of PostgreSQL-V's merge pthread pool.

They do not count SQL backend processes. `connection_pool_size` controls how
many persistent libpq connections, and therefore how many PostgreSQL client
backend processes, the adapter creates. PostgreSQL auxiliary processes are
separate again. All four option values must be positive, must be range-checked,
and must be recorded in benchmark output.

### Maintenance handling

Do not clean up or generalize the maintenance API as part of this integration.
FreshDiskANN, OdinANN, and PostgreSQL-V have materially different maintenance
lifecycles, and a single policy enum or parameterless `merge()` would hide
important behavior. Future baselines may introduce more models that do not fit
such an abstraction.

Keep `ANNIndex::merge(std::size_t thread_count)` unchanged. Also keep the
baseline-specific preprocessor checks in the benchmark kernels. Extend those
checks explicitly for PostgreSQL-V and add comments at every maintenance branch
that describe why each baseline is included or excluded. For example:

```cpp
#if defined(DYNAMIC_ANN_BENCHMARK_FRESHDISKANN)
  // FreshDiskANN requires a kernel-driven merge concurrent with inserts.
  auto merge_future = std::async(
      std::launch::async, [&]() { index.merge(merge_threads); });
#elif defined(DYNAMIC_ANN_BENCHMARK_ODINANN)
  // OdinANN has different merge scheduling and is intentionally excluded here.
  (void) merge_threads;
#elif defined(DYNAMIC_ANN_BENCHMARK_POSTGRESQLV)
  // PostgreSQL-V flushes, merges, and rebuilds in server background workers.
  // Do not launch an adapter-side merge for this workload.
  (void) merge_threads;
#endif
```

The exact branch bodies may differ between insertion and mixed-workload
kernels; the comments must document the semantics at each site rather than
assuming the same treatment everywhere.

`PostgreSQLVIndex::merge(thread_count)` will be a documented no-op for interface
compatibility. It ignores `thread_count` because PostgreSQL-V's background
workers are configured through `LoadOptions` and PostgreSQL settings. The
normal kernels should exclude PostgreSQL-V from explicit merge scheduling, so
the no-op is primarily useful to keep shared code compilable. Do not reinterpret
it as a maintenance barrier during this milestone.

If deterministic maintenance status or waiting becomes necessary, add a
PostgreSQL-V-specific diagnostic or administrative API outside `ANNIndex`.
Reconsidering a common maintenance abstraction should be a separate design task
after the behavior of all existing baselines has been documented and tested.

## Manifest design

Add a common manifest header containing fields required by all benchmark
frontends:

```json
{
  "schema_version": 3,
  "baseline": "postgresqlv",
  "scalar_type": "float32",
  "dimensions": 96,
  "metric": "l2",
  "initial_count": 5000000,
  "engine": {
    "postgres_major": 17,
    "postgres_version": "17.x",
    "extension_version": "0.8.0",
    "postgresqlv_commit": "0d23798b1cc822dfadfda552cd0cd62ae49978be",
    "index_kind": "hnsw"
  },
  "build_options": {
    "graph_degree": 16,
    "build_list_size": 40,
    "memtable_capacity": 50000
  }
}
```

Do not store an absolute PostgreSQL installation path, socket path, port,
process ID, credentials, or the artifact's original absolute path.

Introduce a baseline-independent `read_index_metadata(index_directory)` that
returns the baseline, scalar type, dimensions, metric, and initial count. Update
both benchmark frontends to use it. They currently obtain the initial count by
reading the DiskANN tag sidecar, which cannot work for PostgreSQL-V.

FreshDiskANN and OdinANN manifest version 2 should remain readable. New builders
may emit version 3, or PostgreSQL-V may initially use its own strictly validated
schema while the common migration is completed.

## PostgreSQL 17 installation on Arch

### Package source and pinning

Use the AUR `postgresql17` package base rather than manually configuring a
PostgreSQL source build. At the time of this audit, it is a split package that
produces `postgresql17`, `postgresql17-libs`, and optional documentation, and
installs into `/opt/postgresql17`.

Do not depend on a nominally generic PostgreSQL 17 Linux tarball. PostgreSQL's
official project distributes source and delegates normal binary packaging to
operating-system vendors; server and extension binaries depend on the target
libc, OpenSSL, ICU, compression libraries, LLVM configuration, filesystem
layout, and extension ABI. A package built by AUR may be cached and reused
across containers with the same pinned Arch base and dependency versions, but
it is not a supported cross-distribution binary artifact. Record package
checksums if the resulting Arch packages are promoted into a reusable runtime
image.

Pin the AUR package repository commit in the container build documentation. The
audited AUR HEAD was:

```text
d8f831d74045000be431f37eda0b90a46e20aa13
```

The AUR package builds PostgreSQL from source internally, but it supplies the
Arch packaging, isolated prefix, client libraries, server headers, `pg_config`,
PGXS, and runtime executables. No project-owned PostgreSQL build script is
needed.

### Installation procedure

The container provisioning step should:

1. Install `base-devel`, Git, `patchelf`, and the AUR package's declared build
   dependencies with `pacman`.
2. Create an unprivileged package-build user.
3. Clone the pinned `postgresql17` AUR repository as that user.
4. Run `makepkg --cleanbuild`. The full package checks are desirable in the
   reusable runtime image; `--nocheck` may be used only for a disposable
   development image followed by PostgreSQL-V's own tests.
5. Install the generated `postgresql17-libs` and `postgresql17` packages with
   `pacman -U` as root.
6. Keep the AUR-built package files or record their checksums so the runtime can
   be reproduced.

Verify the installation before building PostgreSQL-V:

```sh
/opt/postgresql17/bin/pg_config --version
test -x /opt/postgresql17/bin/postgres
test -x /opt/postgresql17/bin/initdb
test -f /opt/postgresql17/include/server/postgres.h
test -f /opt/postgresql17/lib/pgxs/src/makefiles/pgxs.mk
```

Configure the extension with:

```sh
PG_CONFIG=/opt/postgresql17/bin/pg_config
```

Use `pg_config --bindir`, `--libdir`, `--pkglibdir`, and `--sharedir` rather
than reconstructing paths in C++. The project CMake configuration should cache
the selected `pg_config` path and generate runtime defaults from its output.

## PostgreSQL-V dependency and build work

The PostgreSQL-V submodule points to the project fork. Make the following
upstream-specific changes in that submodule and commit them there. Then update
the submodule pointer and commit the integration-facing adapter, CMake, test,
documentation, and benchmark changes in focused superproject commits:

1. Replace developer-specific paths with overridable Make variables:
   `PG_CONFIG`, `KNOWHERE_ROOT`, `FOLLY_ROOT`, `GLOG_ROOT`, and
   `NLOHMANN_ROOT`.
2. Pin the exact compatible Knowhere commit and its Faiss dependency.
3. Pin Folly, glog, gflags, and nlohmann-json versions or consume them through
   stable system/CMake package metadata where compatible.
4. Remove absolute home-directory RPATHs. Use a private runtime library
   directory with relocatable `$ORIGIN` entries. The runtime helper currently
   installs non-system Conan shared objects in
   `$(pg_config --pkglibdir)/postgresqlv-runtime`.
5. Ensure extension installation targets `/opt/postgresql17` rather than the
   system PostgreSQL 18 installation.
6. Run the PostgreSQL-V extension regression tests against the selected PG17
   installation before integrating the adapter.

The extension is named `vector`, so it must not overwrite an existing pgvector
installation. The `/opt/postgresql17` prefix isolates it from the system
PostgreSQL package. The implemented helper pins Knowhere to
`8103a1f6feded53f3bc784c5b97abed4f6ffdd56`, stages the generated headers and
libraries under `/opt/postgresqlv-runtime`, patches every bundled non-system
library to `$ORIGIN`, and fails if `ldd vector.so` reports an unresolved
library. It uses Conan's current GCC-15 settings schema with the host GCC 16
compiler; this compatibility normalization is intentional and recorded in the
script.

## PostgreSQL-V configuration patches

Implement these patches in priority order.

### P0: Reproducible build

The build-variable and dependency pinning changes above are required before any
adapter test can be repeatable.

### P1: Search candidate GUC

Add a USERSET session GUC such as `postgresqlv.search_candidates` and replace
the hard-coded HNSW `top_k = 150`. Validate it in `1..MAX_TOPK`. The adapter sets
it once for every pool connection and requires every call's `k` to be no larger
than this value.

Continue using the existing `hnsw.ef_search` GUC for `search_list_size`, and
require `search_list_size >= search_candidates` because Knowhere requires HNSW
`ef` to be at least its requested top-k.

### P2: Background segment build parameters

Implemented: PostgreSQL-V persists the structural HNSW `m` and
`ef_construction` values in its LSM metadata and uses them for later flush and
merge builds. Existing metadata without those fields is rejected rather than
silently rebuilding with unrelated defaults.

### P3: Worker, maintenance, and memory GUCs

Add three `PGC_POSTMASTER` integer GUCs and remove the corresponding source
constants and CPU discovery:

| `LoadOptions` field | PostgreSQL setting | Controls |
| --- | --- | --- |
| `search_worker_threads` | `postgresqlv.search_worker_threads` | outer Folly segment-search executor |
| `maintenance_worker_threads` | `postgresqlv.maintenance_worker_threads` | flush/rebuild pthread pool |
| `background_merge_threads` | `postgresqlv.merge_worker_threads` | merge pthread pool |

The load constructor writes these values into the private cluster's generated
configuration before starting the postmaster. Create every pool from the
validated GUC value; do not use `std::thread::hardware_concurrency()`, `nproc`,
`sysconf(_SC_NPROCESSORS_*)`, or a compile-time array bound. Because these pools
are created during worker startup or first use, reject changes that would need
an in-place resize and require an adapter-managed restart instead.

Deletion rebuild ratio, memtable capacity, and mmap cold-start mode are also
effective postmaster settings. The four-billion-byte request is replaced by the
calculated static layout described above. `memtable_capacity` changes usable
capacity, not the reserved shared-memory footprint.

The mmap-first restart path was exercised and found to race a concurrent search
with its background mmap-to-RAM replacement in PostgreSQL-V v2.0, causing the
vector worker to segfault. `LoadOptions::mmap_cold_start` is therefore `false`
by default. It remains an explicit experimental switch rather than being
removed; a benchmark using it must label the result and validate that upstream
race independently.

### P4: Optional maintenance status/barrier

Expose a SQL status function reporting mutable rows, sealed memtables, queued
flushes, queued merges/rebuilds, and the current index generation. An optional
barrier function should wait for work already scheduled for one index. This is
useful for deterministic tests but is not required for normal benchmark
operation. Keep this as a PostgreSQL-V-specific administrative facility; do not
map it to `ANNIndex::merge()` in this integration.

### P5: Native dependency thread containment

Treat the three PostgreSQL-V worker settings as top-level concurrency budgets,
and derive native dependency pool sizes from them rather than fixing native
pools at one or adding hardware-derived defaults. Current Knowhere HNSW queues
one task per query onto its global search pool, including the single-query
dataset PostgreSQL-V creates for each segment. A one-thread Knowhere search
pool would therefore serialize PostgreSQL-V's outer search executor.

At initialization of each PostgreSQL process that can call Knowhere, use the API
provided by the pinned compatible Knowhere version to set its pool sizes before
the pools are first created. The pinned Knowhere exposes
[`KnowhereConfig::SetSearchThreadPoolSize()` and
`SetBuildThreadPoolSize()`](https://github.com/zilliztech/knowhere/blob/8103a1f6feded53f3bc784c5b97abed4f6ffdd56/include/knowhere/comp/knowhere_config.h#L100-L111);
confirm those APIs against the
commit ultimately pinned for PostgreSQL-V rather than assuming current upstream
compatibility. Use this initial derived policy:

- size the Knowhere search pool used by the vector worker from
  `search_worker_threads`, preserving that many active single-query HNSW
  searches while avoiding any host-CPU default
- size each process-local Knowhere build pool from the explicit PostgreSQL-V
  worker budget for the build/add call sites reachable in that process; use
  `BuildOptions::build_threads` during initial construction and the applicable
  maintenance/merge budgets during loaded operation
- document the derivation and verify the sum across PostgreSQL processes,
  because Knowhere's global pools are process-local rather than postmaster-wide
- add a separate advanced native-pool option only if the pinned call graph
  proves that a derived size cannot express the required job concurrency

The implemented configuration call is reached from `HnswIndexInit`, direct
segment `IndexBuild`, indexed segment search, and growing-memtable brute-force
search. Those are distinct PostgreSQL processes in normal operation: Knowhere's
global pools are process-local, so the configured count applies to each process
that reaches the native path, not to the entire postmaster tree as one global
cap. A four-client smoke run with all three PostgreSQL-V worker counts set to
one confirmed that every observed Knowhere build/search pool initialized at one
rather than the host's 12 online CPUs.

Keep nested compute inside an individual Knowhere task single-threaded. Apply
`ScopedSearchOmpSetter(1)` and the corresponding build setter on the thread that
actually invokes the native kernel. A setter on PostgreSQL-V's outer executor
thread cannot be assumed to propagate when Knowhere redispatches the query to a
different pool thread; patch or configure the pinned Knowhere call site where
necessary. The process environment below is the backstop for all OpenMP worker
threads.

Before `initdb`, `pg_ctl`, or `postgres` is launched, give the child process a
controlled environment containing:

```sh
OMP_NUM_THREADS=1
OMP_THREAD_LIMIT=1
OMP_DYNAMIC=FALSE
OMP_MAX_ACTIVE_LEVELS=1
OPENBLAS_NUM_THREADS=1
GOTO_NUM_THREADS=1
BLIS_NUM_THREADS=1
MKL_NUM_THREADS=1
MKL_DYNAMIC=FALSE
```

Only the variables relevant to the BLAS implementation actually linked at
runtime have an effect, but recording the complete environment is harmless.
Inspect `vector.so` with `ldd` or `readelf -d` to identify that implementation
and verify that only one OpenMP runtime is loaded. Do not set OpenMP affinity
variables that could override or fight the benchmark's `taskset`/`numactl`
policy.

This containment preserves concurrency without nested multiplication. With a
search budget of `S`, up to `S` outer PostgreSQL-V tasks may wait on up to `S`
Knowhere search workers, but every Knowhere task gets a one-thread OpenMP team
and one BLAS thread rather than another `S`- or host-sized team. The duplicated
outer and inner executor threads are an implementation cost, not `S * S`
simultaneous search computation. A later upstream cleanup may remove that
double dispatch, but it is not required for the adapter milestone.

Keep initial index construction separately controlled by
`BuildOptions::build_threads`; its mapping must also be audited so that a
PostgreSQL parallel build and a Knowhere/OpenMP build pool do not multiply one
another. If the pinned dependency version cannot provide reliable runtime
controls, add a documented strict build profile with OpenMP disabled and a
single-threaded BLAS, rather than accepting a hardware-derived default.

### CPU/NUMA affinity and PostgreSQL process counts

The adapter starts `postgres` directly as a descendant of the benchmark
process. Linux CPU affinity and NUMA memory policy applied to the
benchmark with `taskset` and `numactl` are then inherited across fork and exec
by the postmaster, SQL backends, PostgreSQL-V background workers, and their
pthreads. Do not route startup through `systemctl`, another already-running
service manager, or an external cluster, because those processes would not
inherit the benchmark's policy. Tests should inspect `Cpus_allowed_list` and
`Mems_allowed_list` in `/proc/<pid>/status` for the postmaster, one SQL backend,
the PostgreSQL-V vector worker, and representative worker threads.

PostgreSQL is primarily process-per-connection, not a fixed request-handler
thread pool. `connection_pool_size = C` therefore creates `C` persistent SQL
backend processes; a leased connection selects which backend handles a request,
and the operating-system scheduler decides when it runs. For the deterministic
runtime profile, set `max_parallel_workers_per_gather = 0` and
`max_parallel_maintenance_workers = 0` so an individual SQL statement cannot
add PostgreSQL parallel-query or parallel-maintenance workers. Keep autovacuum
and extension background-worker settings explicit as well.

`max_worker_processes` and `max_parallel_workers` are upper bounds, not exact
worker counts. PostgreSQL also starts fixed auxiliary processes such as the
checkpointer, background writer, WAL writer, and autovacuum launcher, and
autovacuum workers appear only when needed. Consequently the three
PostgreSQL-V GUCs can make the relevant pthread pool sizes exact, and the client
pool can make the number of persistent request backends exact, but they cannot
turn the whole PostgreSQL process tree into one fixed thread count. Record and
audit that process tree after lazy pools have been initialized.

## Build implementation

`PostgreSQLVIndex::build()` will perform these steps:

1. Read and validate the fbin data and tag headers.
2. Require a nonzero point count and dimension, matching data/tag counts, one
   tag per row, unique tags, and dimensions no greater than 1024.
3. Create a sibling staging directory rather than partially populating the
   requested final directory.
4. Run `/opt/postgresql17/bin/initdb` under the benchmark user.
5. Generate a PostgreSQL configuration fragment containing at least:
   - `shared_preload_libraries = 'vector'`
   - `listen_addresses = ''`
   - a private Unix socket directory
   - sufficient `max_worker_processes`
   - sufficient `max_connections`
   - explicit PostgreSQL-V search, maintenance, and merge worker counts
   - explicit PostgreSQL query/maintenance parallel-worker settings
   - `pgvector.storage_base_dir = '<staging>/segments'`
   - appropriate shared and dynamic shared-memory settings
6. Start `postgres` directly and wait for a successful libpq connection while
   retaining the child PID for shutdown and error reporting.
7. Create the `vector` extension and a fixed internal schema.
8. Create the table:

   ```sql
   CREATE TABLE ann_items (
     tag bigint PRIMARY KEY
       CHECK (tag BETWEEN 0 AND 4294967295),
     embedding vector(<dimensions>) NOT NULL
   );
   ```

9. Stream `(tag, embedding)` rows through text `COPY`. This is sufficient for
   the small-scale runnable milestone. Add PostgreSQL binary COPY before any
   large-dataset or publication-scale evaluation so build time is not dominated
   by float formatting.
10. Run `ANALYZE`.
11. Create the HNSW index with `vector_l2_ops` or `vector_cosine_ops`, mapping
    `graph_degree` to `m` and `build_list_size` to `ef_construction`.
12. Check the row count and use `EXPLAIN` to prove an ANN query selects the HNSW
    index.
13. Issue `CHECKPOINT` and stop PostgreSQL cleanly.
14. Write the manifest only after all database and index validation succeeds.
15. Atomically rename the staging directory into the requested final path.

Failed builds should retain a clearly named diagnostic log but must not leave a
valid-looking manifest or running postmaster.

## Load implementation

The load constructor will:

1. Strictly parse the manifest and validate baseline, scalar type, tag type,
   dimension, metric, PostgreSQL major version, extension identity, and source
   commit.
2. Acquire the exclusive artifact lock.
3. Reject a live postmaster not owned by this adapter invocation. Detect and
   report stale process metadata separately.
4. Regenerate the configuration fragment so `storage_base_dir` follows a moved
   artifact and the three worker counts match `LoadOptions`.
5. Allocate a short Unix socket path and a collision-free local port.
6. Start PostgreSQL and wait until both SQL and PostgreSQL-V background workers
   are ready.
7. Create `connection_pool_size` libpq connections.
8. Apply connection settings and prepare statements.
9. Log the effective PostgreSQL-V pool sizes, PostgreSQL parallel-worker
   settings, native thread limits, and affinity mask.
10. Run a small indexed search as a readiness check when cold-start loading is
   complete enough to serve queries.

The index object should be moveable but not copyable. Destruction must be
best-effort and non-throwing; provide an explicit `close()` for tests that need
to assert shutdown errors.

## SQL operation mapping

### Search

For L2:

```sql
SELECT tag, embedding <-> $1::vector AS distance
FROM ann_items
ORDER BY embedding <-> $1::vector
LIMIT $2;
```

For cosine, use `<=>`. Values must be passed as parameters, never concatenated
into SQL. Fixed internal identifiers can be constants because every artifact
uses the same private schema.

The implementation must:

- validate the query dimension and result spans through `ANNIndex` helpers
- validate `k <= search_candidates <= search_list_size <= 1000`
- pass all values as libpq parameters; text vector parameters/results are
  acceptable for the small-scale smoke milestone, while binary vector and
  result encoding is required before publication-scale performance evaluation
- define behavior when the live table contains fewer than `k` rows
- ensure index scans are selected during the small integration tests

`ANNIndex` result distances now follow the exact-ground-truth convention: L2 is
**squared Euclidean distance** and cosine is `1 - cosine similarity`.
`compute_ground_truth` already emits squared L2 values. PostgreSQL's `<->`
operator remains in `ORDER BY` so that its HNSW operator class is used, while
the projection returned to the adapter squares that Euclidean value. Cosine's
`<=>` value is returned unchanged. The build/load CTest inserts a point,
queries a known 32-dimensional perturbation, and checks the resulting squared
L2 value; it also checks that the inserted tag is selected.

### Insert

Use a prepared, autocommitted statement:

```sql
INSERT INTO ann_items(tag, embedding)
VALUES ($1::bigint, $2::vector);
```

Map duplicate-tag SQLSTATE to the adapter's documented insert failure. Each
benchmark call remains one committed operation; initial build COPY is the only
bulk path.

### Remove

Use:

```sql
DELETE FROM ann_items WHERE tag = $1::bigint;
```

MVCC makes a committed deletion invisible immediately. PostgreSQL-V's physical
deletion bitmap is maintained later through VACUUM. Do not run VACUUM per
remove. Define and test whether removing a missing tag is idempotent or an
error, matching the existing adapters where practical.

### Merge/maintenance

Normal PostgreSQL-V operation uses automatic maintenance and should not launch
an adapter-side merge future. `merge(thread_count)` is a documented no-op and
ignores its argument. PostgreSQL-V maintenance worker counts come from
`LoadOptions` and PostgreSQL settings. If the optional status/barrier facility
is implemented later, expose it separately rather than changing the meaning of
the common merge method in this milestone.

## CMake and target changes

The current root CMake configuration discovers MKL, libaio, liburing, and both
embedded baseline dependency sets unconditionally. Split it into baseline
options:

```cmake
option(DYNAMIC_ANN_ENABLE_ODINANN "Build OdinANN integration" ON)
option(DYNAMIC_ANN_ENABLE_FRESHDISKANN "Build FreshDiskANN integration" ON)
option(DYNAMIC_ANN_ENABLE_POSTGRESQLV "Build PostgreSQL-V integration" OFF)

set(POSTGRESQLV_PG_CONFIG
    "/opt/postgresql17/bin/pg_config"
    CACHE FILEPATH "pg_config used to build and run PostgreSQL-V")
```

Only discover each baseline's dependencies when it is enabled. A PostgreSQL-V
only configuration must not require MKL, libaio, liburing, or jemalloc.

Add a C++20 `postgresqlv_adapter` library linked to libpq, Threads, and
nlohmann-json. Keep libpq and process-lifecycle implementation in `.cpp` files
rather than placing it all in the public header.

Add these targets:

- `build_disk_index_postgresqlv`
- `bench_insert_search_postgresqlv`
- `bench_mixed_workload_postgresqlv`
- `test_build_and_load_postgresqlv`
- `test_postgresqlv_concurrency`

The build/load smoke test includes a clean close/restart cycle. A separate
crash-recovery test remains follow-up work; it is not needed for the initial
small-scale smoke milestone.

Add PostgreSQL-V to the existing baseline-selection preprocessor trees in the
builder, benchmark frontends, and shared integration test. Maintenance checks
must remain baseline-specific at their points of use, with comments explaining
the behavior. A small selected-index alias header may still reduce repeated
type aliases, but it must not centralize or obscure maintenance decisions.

## File-by-file implementation outline

### New files

- `include/baselines/postgresqlv.hpp`: public options and adapter declaration
- `include/index_manifest.hpp`: common manifest metadata, if separated from
  `utils.hpp`
- `src/baselines/postgresqlv.cpp`: adapter operations and validation
- `src/baselines/postgresqlv.cpp`: bounded libpq pool, initdb/postmaster
  lifecycle, artifact lock, manifest, and vector/COPY wire handling. Keeping
  this initially in one translation unit makes the server lifecycle easier to
  audit; it can be split later without changing the public header.
- `tests/postgresqlv_concurrency.cpp`: concurrent data-plane integration test
- `baselines/PostgreSQL-V/postgresqlv/scripts/build_postgresqlv_runtime.sh`:
  repeatable extension/dependency build, but not a replacement for the AUR
  PostgreSQL package

### Existing files to update

- `include/ann_index.hpp`: distance documentation only; retain the existing
  merge signature
- `include/kernels.hpp`: extend baseline-specific maintenance checks for
  PostgreSQL-V and comment the different baseline semantics
- `include/utils.hpp`: common manifest metadata and backward compatibility
- `tools/build_disk_index.cpp`: PostgreSQL-V selection and build options
- `src/bench_insert_search.cpp`: selected adapter and manifest initial count
- `src/bench_mixed_workload.cpp`: selected adapter and manifest initial count
- `tests/build_and_load.cpp`: PostgreSQL-V branch and guarded DiskANN assertions
- `tests/kernels.cpp`: coverage for each baseline-specific maintenance branch
- `CMakeLists.txt`: optional dependencies, adapter library, executables, tests
- benchmark launch scripts: PostgreSQL-V target and runtime logging
- `baselines/PostgreSQL-V/postgresqlv/src/vector.c`: register the three
  postmaster-start worker-count GUCs
- `baselines/PostgreSQL-V/postgresqlv/src/vector_index_worker.c` and its header:
  allocate maintenance/merge pthread pools from the GUCs instead of macros
- `baselines/PostgreSQL-V/postgresqlv/src/vectorindeximpl.cpp`: size the outer
  search executor from its GUC and initialize the pinned Knowhere/OpenMP limits
- `baselines/PostgreSQL-V/postgresqlv/Makefile`: make OpenMP/BLAS linkage
  explicit and remove machine-specific dependency paths

## Test plan

### Unit tests without a server

- manifest serialization, exact fields, and incompatible-version rejection
- BuildOptions and LoadOptions validation
- fbin and tag count validation
- dimension and candidate-limit validation
- text vector and COPY encoding for the runnable milestone
- binary vector and COPY framing when the large-scale path is added
- connection-pool lease, exception, shutdown, and cancellation behavior
- PostgreSQL error and SQLSTATE translation
- validation and serialization of all three PostgreSQL-V worker counts
- rejection of zero, overflow, and out-of-range worker-count values

### Build/load integration test

Use the PostgreSQL-V-specific build/load test to:

1. Generate temporary float vectors and non-identity tags.
2. Invoke `build_disk_index_postgresqlv`.
3. Validate the manifest and PostgreSQL artifact layout.
4. Confirm the builder stopped its postmaster.
5. Load from the artifact path.
6. Check dimensions and invalid query/result arguments.
7. Search and validate tags and distances.
8. Insert and find a new point.
9. Remove the point and verify immediate invisibility.
10. Invoke the no-op `merge(1)` only to verify the interface contract, with a
    comment explaining that server background workers own maintenance.
11. Destroy and reload the adapter.
12. Verify persistence after restart without depending on an explicit merge.

The preprocessor-maintenance branches in the benchmark kernels remain explicit:
FreshDiskANN owns its explicit merge lifecycle, OdinANN has its own handling,
and PostgreSQL-V has no client-triggered merge. Do not replace those branches
with a generalized maintenance abstraction in this milestone.

### Concurrency test

Use four worker threads and four pooled connections for the required smoke test;
larger concurrency tests are optional. Cover:

- search/search concurrency
- insert/search concurrency
- insert/remove/search concurrency
- PostgreSQL-V background maintenance during client work
- connection failure and replacement
- proof that operations are not serialized through a single global adapter
  mutex

Run the test with non-default, distinct pool sizes, such as three search, two
maintenance, and one merge worker, so swapped mappings are detectable. After
the first query and first maintenance action have initialized lazy pools,
enumerate `/proc/<pid>/task`, capture thread names with `ps -L`, and compare them
with the effective GUCs. Also verify that every server process and thread
remains inside the requested CPU and NUMA masks.

Add a dependency-threading test/profile that:

- logs the pinned Knowhere search/build pool sizes and OpenMP maximum team size
- resolves the linked OpenMP and BLAS implementations from `vector.so`
- exercises search and background segment construction before counting threads
- fails if a configured pool silently expands to the host CPU count
- scans every newly pinned Knowhere/Faiss revision for hardware-derived defaults
  such as `hardware_concurrency`, `_SC_NPROCESSORS_*`, `get_nprocs`, and
  `omp_get_max_threads`, then either overrides or documents every reachable use

Do not require a 32-client or large-dataset run for the first milestone.

### Lifecycle and recovery tests

- clean stop and reload
- postmaster termination followed by recovery
- stale PID and socket cleanup
- startup timeout and useful log reporting
- no orphaned postmaster after a constructor or operation failure
- relocated artifact directory
- artifact path containing spaces
- long artifact path with a separately shortened Unix socket directory

### PostgreSQL-V upstream tests

Run the extension's regression suite against `/opt/postgresql17` before the
adapter tests. Treat crashes, hangs, or failed recovery tests as upstream
failures rather than masking them in the client adapter.

### Completed smoke verification

The following was completed in this worktree after installing the patched
extension and its runtime closure:

- PostgreSQL-V-only Debug configuration and parallel build with both embedded
  baselines disabled
- `postgresqlv_dynamic_build_load`: build, manifest validation, load, invalid
  argument checks, squared-L2 result semantics, no-op merge, a persisted insert,
  a persisted delete across two clean restarts, and a separate cosine build/load
  with its cosine HNSW operator-class plan
- `postgresqlv_concurrent_data_plane`: four client threads and four pooled
  connections performing interleaved search/insert/search/remove operations
- insert/search frontend with 256 initial and 256 inserted 32-D float vectors,
  one client search worker, and one worker in each PostgreSQL-V pool
- mixed-workload frontend with the same data, two clients, 50% inserts, and a
  128-vector memtable; its server log showed two flushes and a background
  merge while foreground search was active

The smoke harness honors `DYNAMIC_ANN_KEEP_TEST_ARTIFACTS=1` only for manual
debugging and benchmark reproduction. Its default remains to delete all
temporary data, preserving CTest independence.

### Remaining validation and upstream limitations

- The PostgreSQL-V upstream regression suite, process/NUMA affinity audit, and
  crash-recovery test have not been made release gates for this milestone.
- The v2.0 mmap cold-start upgrade race is why that adapter option defaults to
  off. Do not enable it for a result intended to compare stable dynamic paths.
- There is no PostgreSQL-V maintenance status/barrier API yet. The smoke
  workload proves asynchronous flush/merge scheduling through the server log,
  but cannot measure a precise quiescence barrier or maintenance debt.
- The adapter uses text COPY and text vector parameters. They are adequate for
  a correctness smoke test but must be replaced with binary encodings before a
  build-time or ingest-throughput comparison.
- PostgreSQL-V logs resource-cleanup warnings from its recovery worker during
  restarts, and shutdown while a flush is active can require the adapter's
  timeout fallback. These are upstream lifecycle issues to resolve before
  claiming crash-recovery or graceful-quiescence guarantees.

## Benchmark methodology

The PostgreSQL-V result is an end-to-end system result. It includes libpq, SQL
execution, MVCC, and PostgreSQL-to-vector-worker IPC. Report this distinction
alongside embedded-library baselines.

Use four explicitly named measurement states:

1. **Warm quiescent:** the initial index is loaded, warmed, and has no queued
   maintenance. This is the closest comparison with a direct native HNSW index.
2. **Sustained dynamic:** inserts/searches/deletes run long enough for flush and
   merge work to occur repeatedly. Report time-series throughput, latency,
   recall, segment count, memtable occupancy, and maintenance backlog.
3. **Post-quiescence:** foreground updates stop and native background work is
   allowed to settle through the PostgreSQL-V-specific status/barrier facility
   when available. Report drain time and the resulting search performance.
4. **Cold restart:** measure server readiness, first-query latency, mmap search,
   and the transition to fully loaded segments separately.

Do not present a short foreground interval as sustained throughput if it merely
defers work into an unreported maintenance backlog. Normal timed dynamic runs
must leave PostgreSQL-V maintenance genuinely asynchronous; a quiescence
barrier is permitted only in the separately labeled post-quiescence state.

For the first milestone, these states need only be exercised by a small-scale
smoke benchmark. Use approximately 10,000 initial vectors, a modest dimension
such as 32 or 96, four client connections, and a deliberately small configured
memtable capacity such as 128 or 256. Execute enough inserts and deletes to
force at least two flushes and one merge or rebuild, then verify search recall,
persistence, maintenance progress, and configuration logging. Prefer a bounded
operation count that completes in minutes over a long throughput campaign.
This proves that the adapter and benchmark are runnable; it does not support a
publication-scale performance claim. Large SIFT/DEEP runs, long equilibrium
experiments, and high-client-count sweeps remain follow-up evaluation work.

For the optional same-engine attribution experiment, build direct Knowhere HNSW
and PostgreSQL-V HNSW with the same pinned Knowhere revision, dataset, metric,
`m`, `ef_construction`, search candidate policy, and target recall. Give the
entire PostgreSQL process tree and the native baseline the same CPU/NUMA mask;
count PostgreSQL SQL backends and maintenance workers inside its resource
budget. Report results by category rather than describing an end-to-end system
number as raw HNSW performance.

Every benchmark run must record:

- PostgreSQL version and package identity
- PostgreSQL-V commit
- Knowhere and Faiss commits/versions
- index kind and distance metric
- all BuildOptions and LoadOptions
- client pool and benchmark worker counts
- the three PostgreSQL-V worker counts and observed pool sizes
- PostgreSQL background-process limits and observed process/thread counts
- process CPU/NUMA masks
- OpenMP and linked BLAS thread limits
- `fsync` and `synchronous_commit` settings
- initial and current live row counts
- cold-start time and warm-up policy
- foreground throughput plus per-operation p50, p95, and p99 latency
- time-series memtable occupancy, segment count, and queued maintenance work
- post-workload maintenance drain time and post-quiescence performance
- VACUUM, flush, merge, and rebuild statistics when available

For the runnable integration, the artifact `manifest.json` records all
`BuildOptions` and engine provenance. The `run_start` event records every
`LoadOptions` field, the derived connection limit, the fixed PostgreSQL worker
and statement-parallelism limits, explicit `fsync=on`, POSIX dynamic shared
memory, and the adapter's one-thread OpenMP/BLAS containment profile. It does
not emit the private socket, port, storage path, or process IDs because those
are ephemeral and would make a portable artifact misleading. Observed
process/thread counts, affinity masks, live row counts, and PostgreSQL-V
maintenance counters still require the optional diagnostic/status work before
publication-quality reporting.

Use a local Unix socket and one connection per active benchmark worker. Warm and
cold start must be separate reported modes. Do not silently disable WAL or
durability for throughput results.

The current mixed workload measures inserts and searches but not deletes. Add a
delete-inclusive workload or trace before claiming coverage of PostgreSQL-V's
full dynamic behavior. Recall checkpoints should not force background
maintenance unless the benchmark explicitly describes a quiescent checkpoint.

## Build and run commands after integration

Configure a PostgreSQL-V-only build:

```sh
cmake -S . -B build-postgresqlv \
  -DCMAKE_BUILD_TYPE=Release \
  -DDYNAMIC_ANN_ENABLE_ODINANN=OFF \
  -DDYNAMIC_ANN_ENABLE_FRESHDISKANN=OFF \
  -DDYNAMIC_ANN_ENABLE_POSTGRESQLV=ON \
  -DPOSTGRESQLV_PG_CONFIG=/opt/postgresql17/bin/pg_config
```

Build and test:

```sh
PG_CONFIG=/opt/postgresql17/bin/pg_config \
  baselines/PostgreSQL-V/postgresqlv/scripts/build_postgresqlv_runtime.sh

cmake --build build-postgresqlv --parallel
ctest --test-dir build-postgresqlv \
  --output-on-failure \
  -R postgresqlv
```

Build an artifact:

```sh
./build-postgresqlv/build_disk_index_postgresqlv \
  --data initial.fbin \
  --tags initial.tags.bin \
  --data-type float32 \
  --index-dir indexes/postgresqlv-hnsw \
  --metric l2 \
  --graph-degree 16 \
  --build-list-size 40 \
  --build-threads 8
```

Run the existing benchmark forms through their PostgreSQL-V targets after their
normal workload arguments are supplied:

```sh
./build-postgresqlv/bench_insert_search_postgresqlv ...
./build-postgresqlv/bench_mixed_workload_postgresqlv ...
```

The launch scripts must apply `taskset`/`numactl` to the adapter process that
will create the private postmaster, not only to a client of an already-running
server. Expose the three `LoadOptions` values as baseline-specific CLI/config
arguments: `--pgv-search-worker-threads`,
`--pgv-maintenance-worker-threads`, and
`--pgv-background-merge-threads`. Also expose
`--pgv-connection-pool-size` and `--pgv-search-candidates`, and print their
effective server-side values before timing begins.

## Delivery phases and estimates

### Phase 1: Runtime bring-up

- install AUR PostgreSQL 17
- patch and build PostgreSQL-V dependencies
- install the extension into the isolated prefix
- start a manual cluster and pass upstream smoke tests

Expected wall-clock work: 3–8 hours, dominated by Knowhere compatibility and
compilation. The AUR PostgreSQL build itself should normally be tens of minutes,
with `check-world` potentially taking longer.

### Phase 2: Runnable adapter

- process owner and artifact layout
- manifest
- libpq connection pool
- text COPY build path
- search, insert, remove, automatic maintenance behavior
- builder and basic build/load test

Expected work: 4–7 hours after the runtime is usable.

### Phase 3: Benchmark-runnable integration

- complete text COPY and parameter handling; defer binary wire formats
- CMake dependency isolation
- PostgreSQL-V baseline selection and documented preprocessor maintenance checks
- small-scale concurrent and restart tests
- small-scale benchmark smoke, metadata, and delete-inclusive workload

Expected work: 2–5 hours.

### Phase 4: PostgreSQL-V tuning patches

- search candidate GUC
- persistent background HNSW parameters
- calculated shared-memory sizing
- explicit search, maintenance, and merge pool GUCs
- Knowhere, OpenMP, and BLAS thread containment
- maintenance/status API

Expected work: 4–10 hours, depending on upstream behavior under concurrency.

The expected total is approximately 10–21 hours once the PostgreSQL-V native
dependency set is known to compile, or 16–33 hours if dependency pinning and
upstream debugging must be solved during the integration.

## Implementation authority and commit discipline

The user authorizes Codex to create focused local Git commits inside both
`baselines/PostgreSQL-V` and the benchmark superproject. Separate confirmation
is not required for ordinary commits within this plan. Keep upstream-specific
extension changes in the PostgreSQL-V history and integration-facing changes in
the superproject history.

Follow these rules:

- preserve unrelated user changes and never include them merely to obtain a
  clean worktree
- keep generated PostgreSQL clusters, indexes, sockets, logs, package files,
  credentials, build directories, and benchmark results out of commits
- commit only upstream-specific PostgreSQL-V source, build-system, and upstream
  test changes inside the submodule
- commit adapter APIs, process/artifact ownership, manifests, CMake targets,
  benchmark integration, and shared tests in the superproject
- create the PostgreSQL-V commit before committing its updated submodule pointer
  in the superproject, and keep each pointer update associated with the parent
  integration that requires it
- identify every local, unpublished PostgreSQL-V and superproject commit in the
  handoff; do not imply that a parent commit is distributable until its
  referenced submodule commit is available to collaborators
- use focused lowercase imperative summaries consistent with the repository
  history
- run the relevant build/test subset before each milestone commit where the
  environment permits it, and state explicitly when a commit is an intentionally
  non-runnable dependency or scaffolding step
- do not push, force-push, rewrite existing history, create releases/tags, or
  publish packages without a separate user request
- finish with both commit lists, commands/tests run, remaining uncommitted
  changes, unpublished submodule commits, and any environmental blockers

A reasonable commit sequence is:

1. make PostgreSQL-V dependencies and PG17 paths reproducible
2. make PostgreSQL-V runtime and worker settings configurable
3. contain Knowhere, OpenMP, and BLAS threading
4. update the superproject's PostgreSQL-V pointer and dependency discovery
5. add PostgreSQL-V artifact and process lifecycle support
6. add the libpq connection pool and ANN adapter operations
7. wire PostgreSQL-V builders and benchmark targets
8. add concurrency, persistence, affinity, and thread-containment tests
9. add benchmark metadata and small delete-inclusive smoke coverage

This sequence is guidance; combine or split changes when a different grouping
produces clearer independently reviewable commits.

## Full-evaluation acceptance criteria

The runnable smoke milestone is complete. The following remain the criteria
for a publication-quality HNSW/float32 evaluation; the limitations above mark
which ones still need dedicated work:

1. A PostgreSQL-V-only CMake configuration succeeds without DiskANN libraries.
2. PostgreSQL 17 and PostgreSQL-V versions are pinned and reported.
3. `build_disk_index_postgresqlv` converts temporary fbin/tag input into a
   portable single-directory artifact.
4. The adapter loads only from that artifact path plus centralized LoadOptions.
5. Search, insert, and remove work and persist across restart.
6. A single adapter instance sustains genuinely concurrent operations through a
   multi-connection pool.
7. PostgreSQL-V background maintenance runs without adapter-side serialization
   or a fake explicit merge.
8. Search and update errors are surfaced as useful C++ exceptions.
9. No postmaster or temporary socket is leaked by successful or failed tests.
10. The shared build/load, concurrency, and restart CTests pass repeatedly.
11. The insert/search and mixed-workload benchmark targets complete on the
    small smoke dataset, force background maintenance, and report their complete
    configuration.
12. L2/cosine result semantics and PostgreSQL durability settings are documented
    rather than inferred.
13. Search, maintenance, and merge pool sizes come from the three explicit
    `LoadOptions` values; no reachable PostgreSQL-V pool uses host CPU discovery.
14. Knowhere pools, OpenMP teams, and the linked BLAS are explicitly bounded,
    logged, and verified after lazy initialization.
15. `taskset`/`numactl` masks are inherited by the entire private PostgreSQL
    process tree, and runtime PostgreSQL statement parallelism is disabled for
    the deterministic profile.
16. Small-scale benchmark output labels PostgreSQL-V as an end-to-end system
    baseline and distinguishes warm-quiescent, sustained-dynamic,
    post-quiescence, and cold-restart measurements without making
    publication-scale performance claims.
17. Sustained results report maintenance debt over time rather than ending with
    unreported queued flush, merge, or rebuild work.

## Environment prerequisites

Before implementation begins, the container should provide:

- at least 2 GiB of `/dev/shm`; 8 GiB is the tested project setting for the
  patched extension
- an overall memory limit of at least 12 GiB for comfortable build and runtime
  operation
- network access for Arch/AUR and source dependency retrieval
- enough disk for AUR package builds and Knowhere/Faiss artifacts
- a usable unprivileged build/benchmark user
- valid Git metadata for the superproject and `baselines/PostgreSQL-V`, so local
  commits, submodule pointer updates, and final diffs can be created and
  validated normally

All other package installation, AUR package construction, source patching,
building, testing, and runtime configuration can be performed inside the
container.
