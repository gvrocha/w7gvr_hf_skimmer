#!/bin/sh
# Sourced by bin/jt9, bin/wsprd, bin/decode_ft8.
# Resolves the current OS/arch build of a vendored decoder and execs it.
#
# decoder_bin <submodule> <binary-name>
decoder_bin() {
    submodule=$1
    name=$2
    shift 2
    platform=$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)
    bin_dir=$(cd "$(dirname "$0")/.." && pwd)
    path="$bin_dir/vendor/$submodule/build-$platform/$name"

    if [ ! -x "$path" ]; then
        echo "$name: no build for platform '$platform' (looked in $path)" >&2
        echo "$name: build it first (see README.md Setup)" >&2
        exit 1
    fi

    # Some vendored binaries (csdr) dynamically link against a shared
    # library built alongside them in the same flat build-<platform>/
    # directory, rather than a system package -- nothing on the default
    # search path would find it otherwise. Harmless to add for binaries
    # that don't need it (jt9/wsprd/decode_ft8).
    build_dir=$(dirname "$path")
    export LD_LIBRARY_PATH="$build_dir${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export DYLD_LIBRARY_PATH="$build_dir${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"

    exec "$path" "$@"
}
