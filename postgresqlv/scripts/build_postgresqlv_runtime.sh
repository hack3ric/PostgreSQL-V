#!/usr/bin/env bash
set -euo pipefail

# Build the extension into the isolated PostgreSQL 17 prefix. This is kept
# outside the benchmark CMake graph because Knowhere is a native PostgreSQL-V
# runtime dependency, not a dependency of the libpq adapter itself.

PG_CONFIG="${PG_CONFIG:-/opt/postgresql17/bin/pg_config}"
RUNTIME_ROOT="${POSTGRESQLV_RUNTIME_ROOT:-/opt/postgresqlv-runtime}"
KNOWHERE_SOURCE="${KNOWHERE_SOURCE:-${RUNTIME_ROOT}/src/knowhere}"
KNOWHERE_PREFIX="${KNOWHERE_PREFIX:-${RUNTIME_ROOT}/knowhere-install}"
KNOWHERE_BUILD_DIR="${KNOWHERE_BUILD_DIR:-${KNOWHERE_SOURCE}/build}"
CONAN_COMPILER_VERSION="${POSTGRESQLV_CONAN_COMPILER_VERSION:-15}"
KNOWHERE_COMMIT="8103a1f6feded53f3bc784c5b97abed4f6ffdd56"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXTENSION_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PG_PKGLIBDIR="$(${PG_CONFIG} --pkglibdir)"

for program in git conan cmake make pkg-config ccache patchelf readelf; do
  command -v "${program}" >/dev/null || {
    echo "missing required program: ${program}" >&2
    exit 1
  }
done
test -x "${PG_CONFIG}" || {
  echo "PG_CONFIG is not executable: ${PG_CONFIG}" >&2
  exit 1
}

mkdir -p "${RUNTIME_ROOT}/src"
if [[ ! -d "${KNOWHERE_SOURCE}/.git" ]]; then
  git clone https://github.com/zilliztech/knowhere.git "${KNOWHERE_SOURCE}"
fi
git -C "${KNOWHERE_SOURCE}" fetch --quiet origin "${KNOWHERE_COMMIT}"
git -C "${KNOWHERE_SOURCE}" checkout --detach "${KNOWHERE_COMMIT}"

# CMake 4 rejects several pinned transitive recipes that still declare a
# pre-3.5 compatibility level. A wrapper applies CMake's documented bridge to
# configure operations only; it deliberately leaves --build/--install intact.
TOOL_DIR="$(mktemp -d)"
trap 'rm -rf "${TOOL_DIR}"' EXIT
cat >"${TOOL_DIR}/cmake" <<'EOF'
#!/bin/sh
case "$1" in
  --build|--install|--open) exec /usr/bin/cmake "$@" ;;
  *) exec /usr/bin/cmake -DCMAKE_POLICY_VERSION_MINIMUM=3.5 "$@" ;;
esac
EOF
chmod 755 "${TOOL_DIR}/cmake"

conan profile detect --force
conan remote add milvus \
  https://milvus01.jfrog.io/artifactory/api/conan/default-conan-local2 --force
# Conan's current settings schema ends at GCC 15, while this container's
# system compiler is GCC 16. Keep the package setting pinned to the newest
# supported ABI-compatible value; the compiler executable itself remains the
# host default selected by Conan. Normalizing the generated default profile is
# necessary because Conan parses both host and build profiles before applying
# command-line host settings.
CONAN_DEFAULT_PROFILE="$(conan profile path default)"
CONAN_CACHE_ROOT="${CONAN_CACHE_ROOT:-$(conan config home)}"
sed -i -E \
  -e "s/^compiler\.version=.*/compiler.version=${CONAN_COMPILER_VERSION}/" \
  -e 's/^compiler\.cppstd=.*/compiler.cppstd=gnu20/' \
  "${CONAN_DEFAULT_PROFILE}"
PATH="${TOOL_DIR}:${PATH}" conan install "${KNOWHERE_SOURCE}" \
  --output-folder "${KNOWHERE_BUILD_DIR}" --build=missing \
  -s build_type=Release -s compiler.version="${CONAN_COMPILER_VERSION}" \
  -s compiler.cppstd=gnu20
PATH="${TOOL_DIR}:${PATH}" conan build "${KNOWHERE_SOURCE}" \
  --output-folder "${KNOWHERE_BUILD_DIR}"
# Knowhere's upstream CMake files concatenate CMAKE_INSTALL_PREFIX into their
# DESTINATION values, so `cmake --install --prefix` is not relocatable. Stage
# the generated runtime explicitly instead of relying on that broken install
# rule. The extension build sees this single self-contained prefix.
KNOWHERE_SHARED_LIBRARY="$(find "${KNOWHERE_BUILD_DIR}" -type f -name libknowhere.so -print -quit)"
test -n "${KNOWHERE_SHARED_LIBRARY}" || {
  echo "Knowhere build did not produce libknowhere.so" >&2
  exit 1
}
install -D -m 755 "${KNOWHERE_SHARED_LIBRARY}" \
  "${KNOWHERE_PREFIX}/lib/libknowhere.so"
install -d "${KNOWHERE_PREFIX}/include"
cp -a "${KNOWHERE_SOURCE}/include/knowhere" "${KNOWHERE_PREFIX}/include/"

# Faiss and knowhere_utils are static build products in the pinned Knowhere
# layout. Keep them beside libknowhere so PGXS gets normal -L/-l inputs.
for archive in libfaiss.a libknowhere_utils.a; do
  archive_path="$(find "${KNOWHERE_BUILD_DIR}/Release" -name "${archive}" \
    -type f -print -quit)"
  test -n "${archive_path}" || {
    echo "Knowhere build did not produce ${archive}" >&2
    exit 1
  }
  install -D -m 644 "${archive_path}" "${KNOWHERE_PREFIX}/lib/${archive}"
done

GENERATOR_DIR="${KNOWHERE_BUILD_DIR}/Release/generators"
test -f "${GENERATOR_DIR}/conanbuild.sh" || {
  echo "missing Conan build environment: ${GENERATOR_DIR}/conanbuild.sh" >&2
  exit 1
}
# shellcheck disable=SC1090
source "${GENERATOR_DIR}/conanbuild.sh"
export PKG_CONFIG_PATH="${GENERATOR_DIR}${PKG_CONFIG_PATH:+:${PKG_CONFIG_PATH}}"
# The Conan generator emits component names for Folly and glog, rather than
# the recipe names. pkg-config expands their transitive include/link flags.
pkg-config --exists milvus-common libfolly libglog gflags nlohmann_json

DEPS_MK="${RUNTIME_ROOT}/postgresqlv-deps.mk"
{
  printf 'PGV_DEP_CPPFLAGS := %s\n' \
    "$(pkg-config --cflags milvus-common libfolly libglog gflags nlohmann_json)"
  printf 'PGV_DEP_SHLIB_LINK := %s\n' \
    "$(pkg-config --libs milvus-common libfolly libglog gflags nlohmann_json)"
} >"${DEPS_MK}"

make -C "${EXTENSION_DIR}" clean all install \
  PG_CONFIG="${PG_CONFIG}" KNOWHERE_ROOT="${KNOWHERE_PREFIX}" \
  KNOWHERE_SOURCE="${KNOWHERE_SOURCE}" \
  POSTGRESQLV_DEPS_MK="${DEPS_MK}"

# vector.so uses $ORIGIN/postgresqlv-runtime. Bundle only non-system shared
# libraries resolved from Conan, add SONAME aliases, and remove Conan's
# absolute build RPATHs. taskset/numactl affinity is unaffected.
RUNTIME_LIBDIR="${PG_PKGLIBDIR}/postgresqlv-runtime"
install -d "${RUNTIME_LIBDIR}"
copy_runtime_library() {
  local library="$1"
  local soname

  install -D -m 755 "${library}" "${RUNTIME_LIBDIR}/$(basename "${library}")"
  soname="$(readelf -d "${library}" | awk -F'[][]' '/SONAME/ { print $2; exit }')"
  if [[ -n "${soname}" && "${soname}" != "$(basename "${library}")" ]]; then
    ln -sf "$(basename "${library}")" "${RUNTIME_LIBDIR}/${soname}"
  fi
  patchelf --set-rpath '$ORIGIN' "${RUNTIME_LIBDIR}/$(basename "${library}")"
}

while read -r library; do
  [[ -n "${library}" ]] || continue
  copy_runtime_library "${library}"
done < <(ldd "${KNOWHERE_PREFIX}/lib/libknowhere.so" |
  awk -v cache_root="${CONAN_CACHE_ROOT}" 'index($3, cache_root) == 1 { print $3 }')

# Some Conan shared libraries have shared-library dependencies of their own.
# Expand that closure so loading vector.so never depends on the Conan cache.
while :; do
  added_library=0
  for staged_library in "${RUNTIME_LIBDIR}"/*.so*; do
    [[ -e "${staged_library}" ]] || continue
    while read -r needed; do
      [[ -e "${RUNTIME_LIBDIR}/${needed}" ]] && continue
      source_library="$(find "${CONAN_CACHE_ROOT}/p" -type f -name "${needed}" -print -quit)"
      [[ -n "${source_library}" ]] || continue
      copy_runtime_library "${source_library}"
      added_library=1
    done < <(readelf -d "${staged_library}" |
      awk -F'[][]' '/NEEDED/ { print $2 }')
  done
  [[ "${added_library}" -eq 1 ]] || break
done
patchelf --set-rpath '$ORIGIN' "${KNOWHERE_PREFIX}/lib/libknowhere.so"
install -D -m 755 "${KNOWHERE_PREFIX}/lib/libknowhere.so" \
  "${RUNTIME_LIBDIR}/libknowhere.so"

ldd "${PG_PKGLIBDIR}/vector.so"
if ldd "${PG_PKGLIBDIR}/vector.so" | grep -q 'not found'; then
  echo "PostgreSQL-V runtime has unresolved shared libraries" >&2
  exit 1
fi
echo "PostgreSQL-V runtime installed with ${PG_CONFIG}"
