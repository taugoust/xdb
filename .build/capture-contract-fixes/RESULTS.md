# R5 capture-contract fixes

Branch: `resident-service-simulation-control`, starting at `01d560aabadaf8c3f563999a1383d721a8bf4364`.

## Delivered

- `xdb ila wait` no longer trusts the stale ChipScoPy `samples_captured=0` when status says capture is full. It uses waveform window size/count when exposed; otherwise, if both requested samples per window and captured window count are known, reports their product. If needed metadata is absent it leaves the existing sample count untouched. No count is inferred for a non-full capture.
- Upload JSON and the waveform manifest now expose observed `trigger_positions` from ChipScoPy's waveform. The singular `trigger_position` is the first actual position, or `null` when unavailable; `trigger_positions` is `null` when metadata is absent (an empty list remains an observed empty list). Upload does not fabricate configured probe-trigger expressions: `triggers` is `null` because this separate command cannot recover those expressions from the waveform. Arm results continue to report their configured trigger expressions.
- ChipScoPy programming now sets the vendor per-call `show_progress_bar=False` to avoid its rich progress bar. This alone does **not** fix JSON stdout: see unresolved item below.

## Evidence

Read-only saved capture evidence at `/scratch/theo/qshell-project/.build/r5-rose-bringup/live-roundtrip/fabric-capture` confirms `wait.json` says full, 2048 requested, one window, zero samples; `upload.json` reports 2048 samples but `triggers: []`; CSV contains `TRIGGER=1` at sample 128. `arm.json` records the trigger position and configured probe comparisons. No hardware was accessed.

## Program output investigation (unresolved)

`program.json` begins with vendor `--> INFO: Programming device with:` output and a `Device program progress ...` bar before its JSON object. Inspected the Nix-provided ChipScoPy 2025.2.0.55 source at `chipscopy/api/device/device.py`: `Device.program(..., show_progress_bar=True)` has a per-call flag to suppress the Rich bar, but unconditionally calls the module-global `printer(...)` at approximately line 887. `chipscopy/utils/printer.py` owns a singleton `Printer` whose `Console` defaults to stdout and has no call-specific output stream. Setting its console output or redirecting stdout is process-global mutable state; this patch does not do so.

A CLI-entry-only configuration of that singleton before programming and before worker threads start may be viable, but must preserve the real stdout for final JSON and must be compatible with the exact ChipScoPy versions (including Vivado 2025.1 consumers). This proposal was not implemented. No claim is made that the CLI now emits clean JSON for ChipScoPy programming.

## Validation

`nix flake check` from this worktree passed all checks on x86_64-linux (tests, formatting, Ruff, Pyright, package/devshell evaluation). Behavioral regressions use ChipScoPy mocks; no hardware was accessed. The saved capture evidence is corroborating evidence, not a live reproduction.
