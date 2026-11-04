# Shared by the scripts in this directory. Source it; don't run it.

# helper <name>: compiles <name>.swift from this directory into a per-user cache,
# rebuilding when the source changes, and prints the binary's path. Fails if swiftc
# is missing (it comes with the Xcode Command Line Tools) or compilation fails.
helper() {
  local src bin
  src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$1.swift"
  bin="${TMPDIR:-/tmp}/mac-input-lag/$1"
  command -v swiftc >/dev/null 2>&1 || return 1
  if [ ! -x "$bin" ] || [ "$src" -nt "$bin" ]; then
    mkdir -p "$(dirname "$bin")" && swiftc -O -o "$bin" "$src" >/dev/null 2>&1 || return 1
  fi
  echo "$bin"
}
