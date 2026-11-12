# WireGuardKit (vendored)

WireGuardKit 1.0.15-26 from https://git.zx2c4.com/wireguard-apple (commit 10da5cf), MIT licence (see COPYING).
Only the three library targets are kept. One change: `Sources/WireGuardKitC/WireGuardKitC.h` includes
`<sys/types.h>`, which Xcode 27's explicit modules require for `u_int32_t`/`u_char`. Later upstream tags
declare swift-tools-version 5.3 with `.iOS(.v15)`, which Xcode 27 rejects, and still lack the include.
