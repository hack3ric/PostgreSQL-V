# Building and Running the PostgreSQL-V Benchmark

This guide builds PostgreSQL-V against PostgreSQL 17, builds the benchmark
drivers, and runs the SIFT1M mixed-workload example. Run benchmark and test
commands as an unprivileged user. The adapter creates and manages a private
PostgreSQL cluster inside each index artifact; the distribution PostgreSQL
service does not need to be running.

PostgreSQL-V and pgvector both install an extension named `vector`. Use a
dedicated PostgreSQL 17 installation that does not contain pgvector.

## Get the source

For a new checkout:

```sh
git clone --branch integrate-postgresqlv --recurse-submodules \
  https://github.com/hack3ric/dynamic-vector-db-benchmark.git
cd dynamic-vector-db-benchmark
```

For an existing checkout, initialize the PostgreSQL-V submodule:

```sh
git submodule update --init baselines/PostgreSQL-V
```

A Git checkout is required because CMake records the PostgreSQL-V submodule
commit in every index manifest.

## Install dependencies on Arch Linux

Install the repository packages:

```sh
sudo pacman -Syu --needed \
  base-devel cmake git pkgconf ccache patchelf binutils \
  openblas nlohmann-json boost argparse curl sysstat numactl
```

The runtime builder requires Conan 2. Install the `conan` AUR package if the
command is not already available:

```sh
git clone https://aur.archlinux.org/conan.git /tmp/conan-aur
cd /tmp/conan-aur
makepkg --cleanbuild --syncdeps --install
cd -
conan --version
```

Build the audited `postgresql17` AUR revision as an unprivileged user, then
install its split packages:

```sh
git clone https://aur.archlinux.org/postgresql17.git /tmp/postgresql17-aur
cd /tmp/postgresql17-aur
git checkout d8f831d74045000be431f37eda0b90a46e20aa13
makepkg --cleanbuild --syncdeps
sudo pacman -U ./*.pkg.tar.zst
cd -
```

Select its isolated PostgreSQL installation:

```sh
export PG_CONFIG=/opt/postgresql17/bin/pg_config
```

## Install dependencies on Debian or Ubuntu

Install the build dependencies. `libargparse-dev` is available in Debian 13
and Ubuntu 24.04 or newer; older releases need a separately installed
p-ranav/argparse header providing `argparse/argparse.hpp`.

```sh
sudo apt update
sudo apt install -y \
  build-essential cmake git patch pkg-config ccache patchelf binutils \
  libopenblas-dev nlohmann-json3-dev libboost-dev libargparse-dev \
  curl sysstat numactl python3-venv
```

If the distribution repositories do not contain PostgreSQL 17, enable the
PostgreSQL Apt repository:

```sh
sudo apt install -y postgresql-common
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh
sudo apt update
```

Install the versioned server, client, PGXS/server headers, and libpq headers:

```sh
sudo apt install -y \
  postgresql-17 postgresql-client-17 postgresql-server-dev-17 libpq-dev
```

Install Conan 2 in an isolated environment if the distribution does not
provide Conan 2:

```sh
sudo python3 -m venv /opt/conan2
sudo /opt/conan2/bin/pip install 'conan>=2,<3'
export PATH=/opt/conan2/bin:$PATH
conan --version
```

Select the version-specific PostgreSQL executable. Do not use
`/usr/bin/pg_config`: the current adapter expects `postgres` and `initdb` next
to `pg_config`.

```sh
export PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config
```

## Verify PostgreSQL 17

These checks apply to both operating-system families:

```sh
"$PG_CONFIG" --version
test -x "$(dirname "$PG_CONFIG")/postgres"
test -x "$(dirname "$PG_CONFIG")/initdb"
test -f "$("$PG_CONFIG" --includedir-server)/postgres.h"
test -f "$("$PG_CONFIG" --pgxs)"
```

`pg_config --version` must report PostgreSQL 17. Use the same `PG_CONFIG` for
the extension build and the benchmark CMake build.

## Build and install the PostgreSQL-V runtime

The helper builds the pinned Knowhere dependency graph, builds `vector.so`,
installs the extension through PGXS, and places its non-system shared libraries
under PostgreSQL's `pkglibdir`. It exports a corrected local revision of the
pinned Folly Conan recipe before resolving that graph. The correction disables
optional libaio, liburing, BLAKE3, and XXH3 code which upstream Folly otherwise
enables based only on host header visibility, without linking those libraries.
This makes the result independent of unrelated development packages installed
on the build host. The helper writes to system installation directories, so
this step requires installation privileges.

The helper also disables PGXS's optional LLVM bitcode output for `vector.so`.
Knowhere exposes OpenMP headers compiled by the host GCC toolchain, while PGXS
uses Clang for bitcode when PostgreSQL was built with LLVM. The normal loadable
extension is built with GCC and OpenMP and does not require that bitcode copy.

The helper's Conan setting supports compiler versions through GCC 15. Use the
host compiler major when supported and cap newer Arch compilers at 15:

```sh
gcc_major=$(g++ -dumpfullversion -dumpversion | cut -d. -f1)
conan_gcc_major=$gcc_major
if (( conan_gcc_major > 15 )); then
  conan_gcc_major=15
fi

sudo env \
  PATH="$PATH" \
  PG_CONFIG="$PG_CONFIG" \
  POSTGRESQLV_CONAN_COMPILER_VERSION="$conan_gcc_major" \
  POSTGRESQLV_RUNTIME_ROOT=/opt/postgresqlv-runtime \
  baselines/PostgreSQL-V/postgresqlv/scripts/build_postgresqlv_runtime.sh
```

Verify the installed extension and its dynamic-library closure:

```sh
test -f "$("$PG_CONFIG" --pkglibdir)/vector.so"
test -f "$("$PG_CONFIG" --sharedir)/extension/vector.control"
ldd -r "$("$PG_CONFIG" --pkglibdir)/vector.so"
```

`ldd -r` must not report missing libraries or undefined symbols attributed to
a library under `postgresqlv-runtime`. It does report PostgreSQL backend API
symbols attributed to `vector.so`; PostgreSQL resolves those when it loads the
extension. The build helper performs this dependency relocation check and
fails before reporting a successful installation if the runtime closure is
incomplete. The CTest load test below verifies the remaining PostgreSQL
symbols in a running server.

## Build and test the benchmark

Configure into `build/`. The dataset preparation and benchmark launch scripts
use that directory. Disabling the other baselines avoids their libaio,
liburing, jemalloc, and Intel MKL dependencies.

```sh
cmake -S . -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DDYNAMIC_ANN_ENABLE_ODINANN=OFF \
  -DDYNAMIC_ANN_ENABLE_FRESHDISKANN=OFF \
  -DDYNAMIC_ANN_ENABLE_POSTGRESQLV=ON \
  -DPOSTGRESQLV_PG_CONFIG="$PG_CONFIG"

cmake --build build --parallel
ctest --test-dir build --output-on-failure -R postgresqlv
```

Use a fresh build directory when changing PostgreSQL installations so cached
libpq paths cannot refer to the previous installation.

## Prepare data and run the hello-world benchmark

Download SIFT1M, convert it to the benchmark's binary format, and split its
base vectors into initial and insertion halves:

```sh
datasets/sift1m/prepare.sh
```

Run the mixed workload from the repository root. The result directory must not
already exist:

```sh
benchmarks/bench_mixed_workload/hello_world/run.sh \
  postgresqlv results/postgresqlv-hello-world
```

The launcher performs the remaining steps:

1. Build and verify a cached PostgreSQL-V HNSW artifact from the first 500,000
   SIFT vectors.
2. Compute exact ground truth for checkpoint 0 and ten 50,000-vector insertion
   checkpoints.
3. Copy the cached artifact into the result directory because the workload
   mutates its private PostgreSQL cluster.
4. Run up to ten checkpoints with eight foreground clients, a 30 percent insert
   mix, a 180-minute deadline, and PostgreSQL-V's configured search and
   background-maintenance workers.
5. Save the command, environment, benchmark output, `iostat`, and `pidstat`
   output in the result directory.

The initial PostgreSQL-V cache is stored at
`indexes/sift1m/hello_world/postgresqlv/initial`. Move an incompatible cached
artifact aside after changing PostgreSQL, the PostgreSQL-V submodule revision,
or persistent build options so the launcher can build a new one.

For HNSW, `--search-list-size` becomes Knowhere's `ef` and
`--pgv-search-candidates` becomes the per-segment top-k. Knowhere requires
`search_list_size >= search_candidates`; the launcher uses 150 for both. The
adapter rejects an invalid pair before starting PostgreSQL, and the extension
also clamps `ef` for direct SQL callers.

Older launcher revisions used `--search-list-size 64` with 150 candidates. A
run with that pair logs `ef(64) should be larger than k(150)`, followed by an
assertion in `knowhere::expected<...>::value()` and an aborted
`VectorIndexWorker`. Rebuild and reinstall PostgreSQL-V, then rebuild the
benchmark drivers, if a host shows that signature. The current extension checks
Knowhere's result before accessing it, so other search errors are reported in
the PostgreSQL log instead of aborting the server process.

## Uninstall PostgreSQL-V

Finish any benchmark that is using PostgreSQL-V before uninstalling it. Removing
`POSTGRESQLV_RUNTIME_ROOT` alone is insufficient: that directory contains the
Knowhere source, build cache, and staging files, while PGXS installs the
extension into the selected PostgreSQL 17 installation.

First select the same `pg_config` used to install PostgreSQL-V. For Arch:

```sh
export PG_CONFIG=/opt/postgresql17/bin/pg_config
```

For Debian or Ubuntu:

```sh
export PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config
```

Remove `vector.so`, the extension control and SQL files, and installed headers
through PGXS:

```sh
sudo make -C baselines/PostgreSQL-V/postgresqlv uninstall \
  PG_CONFIG="$PG_CONFIG"
```

The runtime builder separately installs Knowhere and its non-system shared
library dependencies beside `vector.so`. Resolve and inspect that exact path
before removing it:

```sh
pgv_runtime_libdir="$("$PG_CONFIG" --pkglibdir)/postgresqlv-runtime"
case "$pgv_runtime_libdir" in
  */postgresqlv-runtime) ;;
  *) echo "Unexpected PostgreSQL-V runtime path: $pgv_runtime_libdir" >&2; exit 1 ;;
esac
printf 'Removing PostgreSQL-V libraries from %s\n' "$pgv_runtime_libdir"
sudo rm -rf -- "$pgv_runtime_libdir"
```

Finally, remove the default dependency build workspace if it is no longer
needed:

```sh
pgv_runtime_root=/opt/postgresqlv-runtime
printf 'Removing PostgreSQL-V build workspace from %s\n' "$pgv_runtime_root"
sudo rm -rf -- "$pgv_runtime_root"
```

Set `pgv_runtime_root` to the value originally passed as
`POSTGRESQLV_RUNTIME_ROOT` if a non-default location was used. These commands do
not remove PostgreSQL 17 packages, benchmark index artifacts, datasets, or
result directories. If PostgreSQL-V was installed over a packaged pgvector
extension, reinstall that pgvector package after removal.
