# NVDA Controller Client 2026.1.1 (x64)

This directory vendors the unmodified x64 Controller Client used by the
Windows x64 release build.

- Source: https://download.nvaccess.org/releases/2026.1.1/nvda_2026.1.1_controllerClient.zip
- Archive SHA-256: `2a3a70f729be0c48120c5f24a81653202827c40a7986e289ffe2275a2b45011f`
- `nvdaControllerClient.dll` SHA-256: `2fe60cf00be929aae32e95c1e1507a20ada4902c8fec273b3cc2d3bf5472932a`
- License: LGPL-2.1; the complete license text is `license.txt` and is copied
  into every portable build and installer.

Only the x64 DLL is included because the installer declares x64-compatible
architectures. Builds for x86, ARM64, or ARM64EC must vendor the corresponding
official DLL and update the packaging configuration; do not choose a DLL from
the host operating-system architecture at runtime.
