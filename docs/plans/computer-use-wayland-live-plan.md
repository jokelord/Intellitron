# Wayland live implementation plan

The user approved implementation and verification of the companion design. Defaults remain disabled; fixture success does not establish live desktop acceptance.

1. Native transport package: implement combined Portal lifecycle, Portal-connected PipeWire capture, mapped libei sender, strict helper control/frame IPC and native tests. Own `native/*`.
2. Native Python client package: implement supervised authenticated startup, typed bindings, sealed-frame reception, event delivery results and cancellation/cleanup tests. Own `native_client.py` and its focused tests.
3. Production composition and safety package: implement session-bound lazy runtime, renderer, actual report/fingerprint verification, fresh pre-dispatch sampling/target difference checks, shared seat serialization, cancellation and post-action recovery; wire startup/disposal and diagnostic configuration. Own other Python computer-use modules, relevant integration hooks and tests.

Define the shared wire/runtime interfaces before integrating. Write focused failing regression tests before changes, then run package tests, combined Python regression tests, native build/CTest and read-only diagnostics. Exercise authorized host desktop checks only where graphical-session access is available; report unavailable/skipped gates explicitly. Preserve prompt/history serialization and default capabilities. Do not use Git or modify dependency sources.
