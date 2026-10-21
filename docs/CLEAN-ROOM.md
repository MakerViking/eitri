# Clean-room policy

Eitri is open source, and anyone must be able to build, audit, and fork it without legal risk from QIDI's closed software. This policy says what we may read, what we may write, and how we keep the two apart. It applies to every contributor, every component, and every tool.

Read this before you touch the display protocol, the camera, the MCU firmware, or any QIDI-specific behavior.

## Principles

1. **Public, licensed sources are the default.** Upstream projects and QIDI's public GPL sources are the preferred inputs. Use them under their license, with attribution.
2. **Closed code is off limits.** No QIDI binary, library, or firmware is read, decompiled, copied, or shipped.
3. **Behavior is documented, not code.** What we publish describes what the hardware does on the wire. It does not reproduce or paraphrase closed implementation.
4. **Evidence is recorded.** Every protocol fact in our documentation says how it was observed.

## What we may use

| Source | Permitted use | Conditions |
|---|---|---|
| Upstream Klipper, Kalico, Moonraker, Katapult, ustreamer, Fluidd, Mainsail, RAUC, Buildroot, Linux, U-Boot, TF-A | Read, build, adapt, and redistribute | Under each project's license. Listed in [`NOTICE`](../NOTICE). |
| QIDI's public GPL sources: `QIDITECH/klipper`, `QIDITECH/moonraker`, `QIDITECH/QIDI_PLUS4` | Read, reference, and adapt | Under the license of the specific repository, with attribution in `NOTICE`. Check the license file in that repository. For `QIDI_PLUS4`, its `LICENSE` file is AGPL-3.0, which differs from the README badge; confirm before adapting code from it. |
| Public datasheets for the RK3328, STM32, UVC, and Nextion/TJC display families | Read and cite | Use the vendor's terms. |
| Public documentation for the TJC/Nextion serial command set | Read and cite | Vendor documentation only. |
| Hardware that **we own**, observed through **standard** interfaces: serial traffic, USB descriptors, `v4l2-ctl`, a logic analyzer, a serial terminal | Observe, record, and publish the behavior | Record the method. Do not extract code or assets from a vendor binary to do it. |
| Our own code, our own tests, our own bench measurements | Anything | Apache-2.0, or GPL-3.0-or-later under `klipper-layer/extras/`. |

## What we must never include

The following must not appear in the repository, in any commit, pull request, issue, wiki page, release artifact, or CI log:

- QIDI binaries of any kind, including `xindi`, `QIDILink`, `makerbase-client`, `box_*` and `aht20_f` Cython modules, `.so` files, and their dependencies.
- QIDI `.tft`, `.HMI`, or other display project files, and any image, font, or string table extracted from them.
- Decompiled code, disassembly, Ghidra or other decompiler output, or any text derived from it.
- Firmware dumps of closed or proprietary firmware, including the stock MCU images and the stock eMMC image.
- Credentials, tokens, serial numbers, and network identifiers from a printer's configuration. Among them: API keys, tokens, and the contents of `config.mksini`.
- Any material that a contributor obtained under a license that forbids reuse.

`.gitignore` blocks the common file types (`*.tft`, `*.HMI`, `*.so`, `*.bin`, `*.img`, `backups/`, `dumps/`, `secrets/`). It is a safety net, not a substitute for judgment. A file that is blocked by `.gitignore` must still never be added with `git add -f`.

## Our reverse-engineering process

Protocol work is split between observation and implementation, so that the implementation can be written from a behavior specification alone.

1. **Observe.** Record traffic, timing, and responses from hardware you own, using standard tools. Keep the raw captures out of the repository, in a private workspace.
2. **Specify.** Write a behavior specification: message formats, sequences, timings, error responses, and the observed meaning of each field. Cite the observation for each claim.
3. **Review.** A second person checks that the specification states only what was observed, and that it contains no code, no assets, and no structure copied from a closed implementation.
4. **Implement.** The implementation is written from the reviewed specification and the public documentation. Implementers do not copy from the closed code, and they do not open decompiler output while they write.
5. **Publish.** Only the specification and our implementation are published. Raw captures and analysis notes stay private.

Where an analysis tool has already produced output from a closed binary, that output stays in the private workspace. It is not summarized into published documentation except as a behavior specification that passed review.

Some notes in the project's private workspace predate the adoption of this policy on 2026-10-21. They were produced by analyzing the stock binaries. They stay private, and no part of them is published until it has been re-derived as a behavior specification and has passed review under steps 2 and 3.

## What we publish

We publish:

- the behavior specification for each protocol we speak, with the method used to observe it;
- our implementation, under its license;
- the public references for everything we use;
- the list of unresolved questions, with what would answer them.

We do not publish:

- raw captures, disassembly, decompiled code, or any analysis notes that quote closed code;
- any display asset, font, or string table from a closed project;
- any measurement that depends on a closed binary's internal behavior, as opposed to what the hardware does on the wire.

## Working with the display protocol

The display speaks a serial command set based on the public TJC/Nextion protocol. Klarity implements the public command set and drives the display's stock program by sending the page and component commands that the stock program already responds to. It never reflashes the display, and it never ships or modifies a `.tft`.

Where the stock display uses a command that the public documentation does not cover, the behavior is recorded as an observation in the private notes. It is documented publicly only after it passes the review in step 3 above.

## Contributor responsibilities

- Do not paste a closed binary's output, a decompiler's output, or a copy of a closed file into an issue, a pull request, or a chat with a model. This includes content that a tool summarized for you.
- If you have seen closed code, say so in the pull request, and do not write code for that same behavior yourself until a maintainer has reviewed the situation.
- If you are not sure whether a source is permitted, ask before you use it.
- Keep your bench data private. Remove serial numbers, network addresses, and tokens before you share a log.

## Reporting a concern

If you believe a change contains closed material, report it privately as described in [`SECURITY.md`](../SECURITY.md). A maintainer will review the change and, if needed, remove the material from the history before any release.

## Relationship to the licenses

This policy is a project rule, not a legal opinion. It does not replace the licenses of the sources we use, and it does not decide legal questions. Where a license and this policy differ, the stricter rule applies. A maintainer should review the policy with counsel before the first public release.
