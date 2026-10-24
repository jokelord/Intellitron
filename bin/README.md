# Precompiled Computer Use helper

`linux-aarch64/` contains the repository-distributed Linux ARM64 helper:

| File | Purpose | Bytes | SHA-256 |
|---|---|---:|---|
| `computer-use-helper` | Native Computer Use capture/input helper | 149944 | `ed1846e714e3f98ea168c1bff4fcb6905afbe246c8a03305de0155175a1594f7` |

The helper was copied from the existing local `Release` build at
`build/computer-use-native/computer-use-helper` on 2026-10-09 without stripping
or rebuilding. Its source remains in `backend/computer_use/native/`. The hash
identifies this snapshot; future rebuilds can differ with compiler, flags,
dependency versions, or source changes.

## Runtime compatibility

The helper is a dynamically linked ELF executable, checked for resolved
shared-library dependencies on Ubuntu 24.04.5 ARM64. ARM64 alone is insufficient:
this binary requires glibc >= 2.38 and runtime libraries for GLib/GIO, JSON-GLib,
PipeWire, libei, X11, XTest, XRandR, and their dependencies.

Use `ldd bin/linux-aarch64/computer-use-helper` to check the target host. Rebuild
from source for other architectures or incompatible library versions. For a
different target platform, CMake installs to
`bin/<lowercase CMAKE_SYSTEM_NAME>-<CMAKE_SYSTEM_PROCESSOR>/`; update
`computer_use.helper_binary` accordingly.

YAML native paths (`helper_binary`, `calibration_report_path`, and
`operator_session_safety_config_path`) resolve relative to the repository root;
absolute overrides remain supported. Runtime configuration receives absolute
paths independently of the backend's working directory.

Keep executable permissions when committing this file. If a GitHub web upload
or checkout loses that permission, restore it locally before use:

```bash
chmod +x bin/linux-aarch64/computer-use-helper
```

## Rebuild and explicitly refresh the distributed helper

Run from the repository root with the required development libraries installed.
The helper uses GLib/GIO, JSON-GLib, PipeWire/SPA, libei, X11, XTest, XRandR,
PkgConfig, CMake, and Ninja.

```bash
cmake -S backend/computer_use/native -B build/computer-use-native \
  -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF
cmake --build build/computer-use-native --target computer-use-helper -j4
cmake --install build/computer-use-native --prefix "$PWD" \
  --component computer-use-helper
```

Use a fresh ignored build directory if an existing one uses another generator.
The named install component copies only the runtime helper executable. Building alone
does not replace the distributed copy; review the updated binary and refresh the
size/hash above after an intentional install. Build intermediates remain
ignored. No shared libraries or test executables are bundled.

The Qt6 `archery-input-target` in `computer_use_fixture/build/` is a local
testing intermediate, requires an additional GUI environment and dependencies,
and remains ignored rather than distributed.

Large downloaded dependencies remain separate: `deploy/searxng/dc` is an
approximately 68.6 MiB Docker Compose download with installed Compose fallbacks,
and wheel outputs, Python environments, vendor source trees, and model weights
remain excluded. They are not small project-native runtime programs.

Compilation and dependency resolution do not prove desktop capture, input
delivery, calibration, or end-to-end acceptance. Continue following
[Computer Use operations](../docs/computer-use-vision-operations.md).
