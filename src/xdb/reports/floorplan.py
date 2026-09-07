from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
import colorsys
import hashlib
import json
import os
import re
import stat
import tempfile
from typing import Any
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr

from xdb.backend.vivado import _run_vivado_tcl
from xdb.errors import XdbError


_SCHEMA = "xdb-floorplan-render-v1"
_RECORD_SCHEMA = "xdb-floorplan-records-v1"
_RECORD_BEGIN = "XDB_FLOORPLAN_BEGIN"
_RECORD_END = "XDB_FLOORPLAN_END"
_INVALID_XML_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_REQUIRED_METADATA = {"schema", "design", "device", "tool_version"}
_REQUIRED_STATS = {
    "primitive_cells",
    "placed_cells",
    "unplaced_cells",
    "sites_with_coordinates",
    "sites_without_coordinates",
    "routing_errors",
}

_CHECKPOINT_CANDIDATES = (
    "checkpoints/shell_routed.dcp",
    "checkpoints/shell_routed_locked.dcp",
    "checkpoints/config_0/shell_routed_c0.dcp",
    "checkpoints/static_routed_locked.dcp",
    "checkpoints/routed.dcp",
    "shell_routed.dcp",
    "routed.dcp",
)

_RESOURCE_ORDER = (
    "logic",
    "bram",
    "uram",
    "dsp",
    "io",
    "transceiver",
    "clock",
    "hard",
    "other",
)
_RESOURCE_LABELS = {
    "logic": "CLB / logic",
    "bram": "block RAM",
    "uram": "UltraRAM",
    "dsp": "DSP",
    "io": "I/O",
    "transceiver": "transceiver",
    "clock": "clocking",
    "hard": "hard IP / NoC",
    "other": "other placed site",
}
# Vivado-Device-View-inspired dark palette. Background sits in the
# #2b2b2b–#333333 range (like the Vivado GUI), and resource types are drawn
# in slightly desaturated pastels that read clearly on the dark surface.
_RESOURCE_COLORS = {
    "logic":       "#3f5468",  # muted steel-blue — CLB columns
    "bram":        "#3b5b7a",  # deeper blue for BRAM
    "uram":        "#4b3f70",  # violet for URAM
    "dsp":         "#7a3a3a",  # rust for DSP
    "io":          "#2f6b46",  # forest green for I/O
    "transceiver": "#7a6a2a",  # olive/gold for GT bricks
    "clock":       "#4a4a4a",  # neutral for clocking
    "hard":        "#4a4a5a",  # slate for hard IP / NoC
    "other":       "#3a3a3a",  # near-background for filler
}
# Theme constants — matched to Vivado 2023.2 Device View defaults (dark grey).
_THEME = {
    "document_bg":  "#1a1a1a",   # near-black canvas (Vivado ~#191919)
    "plot_bg":      "#1a1a1a",   # inner plot surface — same as canvas
    "plot_stroke":  "#3a3a3a",   # thin outline around plot rect
    "ink":          "#e6e6e6",   # main text
    "ink_muted":    "#a0a4ac",   # subtitle / secondary text
    "ink_dim":      "#7c8088",   # captions
    "legend_swatch_stroke": "#3a3a3a",
    "pblock_label": "#e0e0e0",
}
# Vivado Device View "highlight_objects" palette — bright, saturated colors
# on a dark surface. Slot 1 (typically inst_shell — the dominant "background"
# hierarchy) is muted to a warm slate so it recedes; the vFPGA slots stay
# saturated so the outlined regions read clearly.
_GROUP_COLORS = (
    "#3c78ff",   # blue        (slot 0 — usually <top>, unnoticeable)
    "#5a5040",   # muted warm slate — inst_shell background
    "#a3803a",   # dusty gold — inst_static (moderate visibility)
    "#22c825",   # green       (vFPGA 0)
    "#ffee00",   # yellow      (vFPGA 1)
    "#00c8c8",   # cyan        (vFPGA 2)
    "#ff36ff",   # magenta     (vFPGA 3)
    "#a0f000",   # lime        (vFPGA 4)
    "#ff8000",   # dark orange (vFPGA 5)
    "#00a0ff",   # sky blue
    "#c848ff",   # purple
    "#ff6060",   # coral
    "#40e0d0",   # turquoise
    "#ffd700",   # gold
    "#98fb98",   # pale green
    "#ff69b4",   # hot pink
    "#7fffd4",   # aquamarine
    "#dda0dd",   # plum
    "#f0e68c",   # khaki
    "#87cefa",   # light sky
)
_MAX_PATH_RECTANGLES = 1024
_MIN_DOCUMENT_WIDTH = 1100.0
_MARK_SIZE = {
    "logic": (1.25, 1.25),
    "bram": (2.2, 3.4),
    "uram": (2.5, 4.2),
    "dsp": (2.2, 3.2),
    "io": (2.0, 2.0),
    "transceiver": (3.2, 5.0),
    "clock": (1.8, 1.8),
    "hard": (4.0, 5.5),
    "other": (1.8, 1.8),
}


_VIVADO_FLOORPLAN_TCL = r"""
proc xdb_field {value} {
  return [string map [list "\t" " " "\n" " " "\r" " "] $value]
}
proc xdb_prop {object name} {
  if {[catch {set value [get_property $name $object]}]} { return "" }
  return $value
}
proc xdb_group {name depth} {
  # Special case: if the cell lives inside inst_user_wrapper_N, name the group
  # after that wrapper (so each vFPGA is its own color regardless of depth).
  if {[regexp {(inst_user_wrapper_\d+)} $name _ wrapper]} {
    return $wrapper
  }
  set parts [split $name "/"]
  if {[llength $parts] <= 1} { return "<top>" }
  return [join [lrange $parts 0 [expr {$depth - 1}]] "/"]
}

set dcp [lindex $argv 0]
set output [lindex $argv 1]
set hierarchy_depth [lindex $argv 2]
if {![string is integer -strict $hierarchy_depth] || $hierarchy_depth < 1} {
  error "hierarchy depth must be a positive integer"
}

open_checkpoint $dcp
set stream [open $output "w"]
fconfigure $stream -encoding utf-8 -translation lf
puts $stream "XDB_FLOORPLAN_BEGIN"
puts $stream "META\tschema\txdb-floorplan-records-v1"
puts $stream "META\tdesign\t[xdb_field [current_design]]"
puts $stream "META\tdevice\t[xdb_field [xdb_prop [current_design] PART]]"
puts $stream "META\ttool_version\t[xdb_field [version -short]]"

array set occupancy {}
set primitive_count 0
set placed_count 0
set unplaced_count 0
set cells [get_cells -hierarchical -quiet -filter {IS_PRIMITIVE == 1}]
set cell_names [get_property NAME $cells]
set cell_locs [get_property LOC $cells]
foreach cell $cells name $cell_names loc $cell_locs {
  incr primitive_count
  if {$loc eq ""} {
    incr unplaced_count
    continue
  }
  incr placed_count
  set group [xdb_group $name $hierarchy_depth]
  set key "$loc\u001f$group"
  if {[info exists occupancy($key)]} {
    incr occupancy($key)
  } else {
    set occupancy($key) 1
  }
}

set sites [get_sites -quiet]
set site_names [get_property NAME $sites]
set site_types [get_property SITE_TYPE $sites]
# Physical tile coordinates instead of RPM grid — matches Vivado Device View
# geometry. Prior version used a single bulk `get_tiles -of_objects $sites`
# and relied on element-wise ordering, which Vivado does NOT guarantee: with
# duplicate or reordered tiles the returned list mis-aligns with $sites and
# every SLICE ends up stamped with some other site's tile coords, wrecking
# per-vFPGA bboxes. Correct approach: build a tile-name -> (col,row) lookup
# once, then per-site fetch the containing tile's NAME.
set all_tiles [get_tiles -quiet]
array set tc {}
array set tr {}
foreach n [get_property NAME $all_tiles] c [get_property COLUMN $all_tiles] r [get_property ROW $all_tiles] {
    set tc($n) $c
    set tr($n) $r
}
set site_xs [list]
set site_ys [list]
foreach s $sites {
    set t [lindex [get_tiles -of_objects $s -quiet] 0]
    if {$t ne ""} {
        set tn [get_property NAME $t]
        if {[info exists tc($tn)]} {
            lappend site_xs $tc($tn)
            lappend site_ys $tr($tn)
            continue
        }
    }
    lappend site_xs ""
    lappend site_ys ""
}
unset tc tr
set coordinate_count 0
set missing_coordinate_count 0
foreach site $sites name $site_names type $site_types x $site_xs y $site_ys {
  if {$x eq "" || $y eq ""} {
    incr missing_coordinate_count
    continue
  }
  incr coordinate_count
  puts $stream "SITE\t[xdb_field $name]\t[xdb_field $type]\t$x\t$y"
}

foreach key [lsort [array names occupancy]] {
  set fields [split $key "\u001f"]
  set site [lindex $fields 0]
  set group [join [lrange $fields 1 end] "\u001f"]
  puts $stream "OCC\t[xdb_field $site]\t[xdb_field $group]\t$occupancy($key)"
}

set route_report [report_route_status -return_string]
if {![regexp -nocase {# of nets with routing errors[^0-9]*([0-9,]+)} $route_report _ routing_errors]} {
  error "could not determine routing-error count from report_route_status"
}
set routing_errors [string map {, ""} $routing_errors]

foreach pblock [lsort [get_pblocks -quiet]] {
  set name [xdb_prop $pblock NAME]
  set ranges [xdb_prop $pblock GRID_RANGES]
  puts $stream "PBLOCK\t[xdb_field $name]\t[xdb_field $ranges]"
}
puts $stream "STAT\tprimitive_cells\t$primitive_count"
puts $stream "STAT\tplaced_cells\t$placed_count"
puts $stream "STAT\tunplaced_cells\t$unplaced_count"
puts $stream "STAT\tsites_with_coordinates\t$coordinate_count"
puts $stream "STAT\tsites_without_coordinates\t$missing_coordinate_count"
puts $stream "STAT\trouting_errors\t$routing_errors"
puts $stream "XDB_FLOORPLAN_END"
close $stream
close_design
exit 0
"""


@dataclass(frozen=True)
class FloorplanSite:
    name: str
    site_type: str
    x: int
    y: int
    resource: str


@dataclass(frozen=True)
class FloorplanOccupancy:
    site: str
    group: str
    cells: int


@dataclass(frozen=True)
class FloorplanPblock:
    name: str
    ranges: tuple[str, ...]


@dataclass(frozen=True)
class FloorplanDesign:
    source: Path
    design: str | None
    device: str | None
    tool_version: str | None
    sites: dict[str, FloorplanSite]
    occupancy: tuple[FloorplanOccupancy, ...]
    pblocks: tuple[FloorplanPblock, ...]
    stats: dict[str, int]


def _existing_checkpoint(path: Path, description: str = "routed checkpoint") -> Path:
    checkpoint = path.expanduser()
    if not checkpoint.is_file():
        raise XdbError(f"{description} not found: {path}")
    if checkpoint.suffix.lower() != ".dcp":
        raise XdbError(f"expected a Vivado DCP checkpoint: {checkpoint}")
    return checkpoint


def discover_floorplan_checkpoint(
    path: str | Path,
    *,
    dcp: str | Path | None = None,
) -> Path:
    selected = Path(path).expanduser()
    if not selected.exists():
        raise XdbError(f"path not found: {selected}")

    if selected.is_file():
        if dcp is not None:
            raise XdbError("--dcp can only be used when <path> is a directory")
        return _existing_checkpoint(selected)
    if not selected.is_dir():
        raise XdbError(f"not a file or directory: {selected}")

    if dcp is not None:
        checkpoint = Path(dcp).expanduser()
        if not checkpoint.is_absolute():
            checkpoint = selected / checkpoint
        return _existing_checkpoint(checkpoint)

    for relative in _CHECKPOINT_CANDIDATES:
        candidate = selected / relative
        if candidate.is_file():
            return candidate

    matches = sorted(
        candidate
        for candidate in selected.rglob("*routed*.dcp")
        if candidate.is_file() and "unrouted" not in candidate.name.lower()
    )
    if not matches:
        raise XdbError(f"no routed Vivado DCP checkpoint found under: {selected}")
    if len(matches) > 1:
        listed = ", ".join(str(item.relative_to(selected)) for item in matches[:5])
        suffix = " ..." if len(matches) > 5 else ""
        raise XdbError(
            f"multiple routed checkpoints found; select one with --dcp: {listed}{suffix}"
        )
    return matches[0]


def classify_site_resource(name: str, site_type: str) -> str:
    identity = f"{name} {site_type}".upper()
    if name.upper().startswith("SLICE_") or site_type.upper().startswith(("SLICE", "CLE")):
        return "logic"
    if "RAMB" in identity or "BRAM" in identity:
        return "bram"
    if "URAM" in identity:
        return "uram"
    if re.search(r"(?:^|[ _])DSP(?:48|58|_)", identity):
        return "dsp"
    if re.search(r"(?:^|[ _])(?:GTY|GTH|GTM|GTYP|GTME|GTF|GTP)", identity):
        return "transceiver"
    if re.search(r"(?:^|[ _])(?:HPIO|XPIO|HDIO|IOB|BITSLICE)", identity):
        return "io"
    if re.search(r"(?:^|[ _])(?:BUFG|BUFH|MMCM|PLL|DPLL|XPLL)", identity):
        return "clock"
    if re.search(
        r"(?:^|[ _])(?:PCIE|CMAC|MRMAC|DCMAC|ILKN|HBM|NOC|DDRMC|CIPS|AIE|SYSMON|CONFIG)",
        identity,
    ):
        return "hard"
    return "other"


def _record_payload(text: str, source: Path) -> str:
    start = text.find(_RECORD_BEGIN)
    finish = text.find(_RECORD_END)
    if start < 0 or finish < 0 or finish <= start:
        raise XdbError(f"invalid Vivado floorplan records for {source}: missing record markers")
    return text[start + len(_RECORD_BEGIN) : finish]


def parse_floorplan_records(text: str, source: str | Path) -> FloorplanDesign:
    checkpoint = Path(source)
    metadata: dict[str, str] = {}
    sites: dict[str, FloorplanSite] = {}
    occupancy_counts: Counter[tuple[str, str]] = Counter()
    pblocks: list[FloorplanPblock] = []
    pblock_names: set[str] = set()
    stats: dict[str, int] = {}

    for line_number, raw_line in enumerate(_record_payload(text, checkpoint).splitlines(), start=1):
        line = raw_line.rstrip("\r")
        if not line:
            continue
        fields = line.split("\t")
        kind = fields[0]
        try:
            if kind == "META" and len(fields) == 3:
                if fields[1] in metadata:
                    raise ValueError(f"duplicate metadata field {fields[1]!r}")
                metadata[fields[1]] = fields[2]
            elif kind == "SITE" and len(fields) == 5:
                name, site_type = fields[1], fields[2]
                site = FloorplanSite(
                    name=name,
                    site_type=site_type,
                    x=int(fields[3]),
                    y=int(fields[4]),
                    resource=classify_site_resource(name, site_type),
                )
                if name in sites:
                    raise ValueError(f"duplicate site {name!r}")
                sites[name] = site
            elif kind == "OCC" and len(fields) == 4:
                count = int(fields[3])
                if count <= 0:
                    raise ValueError("occupancy count must be positive")
                key = (fields[1], fields[2])
                if key in occupancy_counts:
                    raise ValueError(f"duplicate occupancy record for {key!r}")
                occupancy_counts[key] = count
            elif kind == "PBLOCK" and len(fields) == 3:
                if fields[1] in pblock_names:
                    raise ValueError(f"duplicate pblock {fields[1]!r}")
                ranges = tuple(item for item in fields[2].split() if item)
                pblocks.append(FloorplanPblock(name=fields[1], ranges=ranges))
                pblock_names.add(fields[1])
            elif kind == "STAT" and len(fields) == 3:
                value = int(fields[2])
                if value < 0:
                    raise ValueError("statistic must not be negative")
                if fields[1] in stats:
                    raise ValueError(f"duplicate statistic {fields[1]!r}")
                stats[fields[1]] = value
            else:
                raise ValueError(f"unsupported or malformed {kind!r} record")
        except ValueError as error:
            raise XdbError(
                f"invalid Vivado floorplan record at line {line_number} for {checkpoint}: {error}"
            ) from error

    if metadata.get("schema") != _RECORD_SCHEMA:
        observed = metadata.get("schema") or "missing"
        raise XdbError(f"unsupported Vivado floorplan record schema for {checkpoint}: {observed}")
    missing_metadata = sorted(_REQUIRED_METADATA - metadata.keys())
    if missing_metadata:
        raise XdbError(
            f"incomplete Vivado floorplan metadata for {checkpoint}: "
            f"missing {', '.join(missing_metadata)}"
        )
    empty_metadata = sorted(key for key in _REQUIRED_METADATA if not metadata[key])
    if empty_metadata:
        raise XdbError(
            f"incomplete Vivado floorplan metadata for {checkpoint}: "
            f"empty {', '.join(empty_metadata)}"
        )
    missing_stats = sorted(_REQUIRED_STATS - stats.keys())
    if missing_stats:
        raise XdbError(
            f"incomplete Vivado floorplan statistics for {checkpoint}: "
            f"missing {', '.join(missing_stats)}"
        )
    if not sites:
        raise XdbError(f"Vivado returned no physical site coordinates for: {checkpoint}")

    occupied_cells = sum(occupancy_counts.values())
    placed_cells = stats.get("placed_cells")
    if placed_cells is not None and occupied_cells != placed_cells:
        raise XdbError(
            f"inconsistent Vivado floorplan records for {checkpoint}: "
            f"occupancy accounts for {occupied_cells} cells, expected {placed_cells}"
        )
    primitive_cells = stats.get("primitive_cells")
    unplaced_cells = stats.get("unplaced_cells")
    if (
        primitive_cells is not None
        and placed_cells is not None
        and unplaced_cells is not None
        and primitive_cells != placed_cells + unplaced_cells
    ):
        raise XdbError(
            f"inconsistent Vivado floorplan records for {checkpoint}: "
            "primitive count does not equal placed plus unplaced cells"
        )
    coordinate_count = stats.get("sites_with_coordinates")
    if coordinate_count is not None and coordinate_count != len(sites):
        raise XdbError(
            f"inconsistent Vivado floorplan records for {checkpoint}: "
            f"received {len(sites)} site coordinates, expected {coordinate_count}"
        )

    occupancy = tuple(
        FloorplanOccupancy(site=site, group=group, cells=count)
        for (site, group), count in sorted(occupancy_counts.items())
    )
    return FloorplanDesign(
        source=checkpoint,
        design=metadata.get("design") or None,
        device=metadata.get("device") or None,
        tool_version=metadata.get("tool_version") or None,
        sites=sites,
        occupancy=occupancy,
        pblocks=tuple(sorted(pblocks, key=lambda item: item.name)),
        stats=stats,
    )


def inspect_floorplan_checkpoint(
    path: str | Path,
    *,
    hierarchy_depth: int = 1,
    timeout: int = 1800,
) -> FloorplanDesign:
    checkpoint = _existing_checkpoint(Path(path))
    if hierarchy_depth <= 0:
        raise XdbError("--hierarchy-depth must be > 0")
    if timeout <= 0:
        raise XdbError("--timeout must be > 0")

    with tempfile.TemporaryDirectory(prefix="xdb-floorplan-") as tmp:
        records = Path(tmp) / "floorplan.tsv"
        result = _run_vivado_tcl(
            _VIVADO_FLOORPLAN_TCL,
            [str(checkpoint), str(records), str(hierarchy_depth)],
            timeout=timeout,
        )
        try:
            text = records.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            detail = result.stdout.strip()
            suffix = f"\nVivado output:\n{detail}" if detail else ""
            raise XdbError(
                f"Vivado did not produce floorplan records: {records}{suffix}"
            ) from error
    design = parse_floorplan_records(text, checkpoint)
    if not design.occupancy or design.stats.get("placed_cells", 0) <= 0:
        raise XdbError(f"checkpoint contains no placed primitives; use a routed DCP: {checkpoint}")
    if design.stats["routing_errors"]:
        # Placement information is still valid even if routing failed; downgrade
        # to a warning when the caller sets XDB_ALLOW_ROUTING_ERRORS=1.
        if os.environ.get("XDB_ALLOW_ROUTING_ERRORS", "") not in ("1", "true", "yes"):
            raise XdbError(
                f"checkpoint contains {design.stats['routing_errors']} routing errors; "
                f"use a fully routed DCP: {checkpoint}"
            )
        else:
            import sys
            print(
                f"warning: checkpoint has {design.stats['routing_errors']} routing "
                f"errors — rendering placement only",
                file=sys.stderr,
            )
    return design


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise XdbError(f"failed to read checkpoint: {path}") from error
    return digest.hexdigest()


def _fmt(value: float) -> str:
    rounded = round(value, 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def _rectangle_path_chunks(
    rectangles: Iterable[tuple[float, float, float, float]],
) -> Iterable[tuple[str, int]]:
    ordered = sorted(
        rectangles,
        key=lambda item: (item[1], item[0], item[2], item[3]),
    )
    for start in range(0, len(ordered), _MAX_PATH_RECTANGLES):
        chunk = ordered[start : start + _MAX_PATH_RECTANGLES]
        commands = [
            f"M{_fmt(x)} {_fmt(y)}h{_fmt(width)}v{_fmt(height)}h-{_fmt(width)}z"
            for x, y, width, height in chunk
        ]
        yield "".join(commands), len(chunk)


def _mark_path_chunks(
    points: Iterable[tuple[float, float]],
    width: float,
    height: float,
) -> Iterable[tuple[str, int]]:
    half_width = width / 2.0
    half_height = height / 2.0
    return _rectangle_path_chunks(
        (x - half_width, y - half_height, width, height) for x, y in points
    )


def _site_occupancy(
    design: FloorplanDesign,
) -> tuple[dict[str, Counter[str]], Counter[str], Counter[str], int]:
    by_site: dict[str, Counter[str]] = defaultdict(Counter)
    missing_cells = 0
    for item in design.occupancy:
        if item.site not in design.sites:
            missing_cells += item.cells
            continue
        by_site[item.site][item.group] += item.cells

    group_cells: Counter[str] = Counter()
    group_sites: Counter[str] = Counter()
    for counts in by_site.values():
        for group, count in counts.items():
            group_cells[group] += count
            group_sites[group] += 1
    return by_site, group_cells, group_sites, missing_cells


def _pblock_raw_rectangles(
    design: FloorplanDesign,
    pblock: FloorplanPblock,
) -> list[tuple[float, float, float, float]]:
    rectangles: list[tuple[float, float, float, float]] = []
    for item in pblock.ranges:
        if ":" not in item:
            continue
        first_name, second_name = item.split(":", 1)
        first = design.sites.get(first_name)
        second = design.sites.get(second_name)
        if first is None or second is None:
            continue
        rectangles.append(
            (
                float(min(first.x, second.x)),
                float(min(first.y, second.y)),
                float(max(first.x, second.x)),
                float(max(first.y, second.y)),
            )
        )
    return rectangles


def _merge_pblock_rectangles(
    rectangles: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    tolerance = 0.0

    def contains(
        outer: tuple[float, float, float, float],
        inner: tuple[float, float, float, float],
    ) -> bool:
        ox0, oy0, ox1, oy1 = outer
        ix0, iy0, ix1, iy1 = inner
        return ox0 <= ix0 and oy0 <= iy0 and ox1 >= ix1 and oy1 >= iy1

    def can_merge(
        first: tuple[float, float, float, float],
        second: tuple[float, float, float, float],
    ) -> bool:
        if contains(first, second) or contains(second, first):
            return True
        x0, y0, x1, y1 = first
        sx0, sy0, sx1, sy1 = second
        x_gap = max(0.0, max(x0, sx0) - min(x1, sx1))
        y_gap = max(0.0, max(y0, sy0) - min(y1, sy1))
        same_y_span = abs(y0 - sy0) <= tolerance and abs(y1 - sy1) <= tolerance
        same_x_span = abs(x0 - sx0) <= tolerance and abs(x1 - sx1) <= tolerance
        return (same_y_span and x_gap <= tolerance) or (same_x_span and y_gap <= tolerance)

    merged = sorted(rectangles, key=lambda item: (item[1], item[0], item[3], item[2]))
    changed = True
    while changed:
        changed = False
        for first_index, first in enumerate(merged):
            for second_index in range(first_index + 1, len(merged)):
                second = merged[second_index]
                if not can_merge(first, second):
                    continue
                merged[first_index] = (
                    min(first[0], second[0]),
                    min(first[1], second[1]),
                    max(first[2], second[2]),
                    max(first[3], second[3]),
                )
                del merged[second_index]
                changed = True
                break
            if changed:
                break
    return merged


def _validate_title(title: str | None) -> None:
    if title is not None and _INVALID_XML_CONTROL.search(title):
        raise XdbError("figure title contains a character that is invalid in XML")


def _group_color(index: int) -> str:
    if index < len(_GROUP_COLORS):
        return _GROUP_COLORS[index]
    hue = (index * 0.6180339887498949) % 1.0
    red, green, blue = colorsys.hls_to_rgb(hue, 0.43, 0.64)
    return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"


def _short_label(value: str, limit: int = 48) -> str:
    if len(value) <= limit:
        return value
    return f"…{value[-(limit - 1) :]}"


def _svg_document(
    design: FloorplanDesign,
    *,
    title: str | None,
    hierarchy_depth: int,
    checkpoint_sha256: str,
    show_pblocks: bool,
    max_groups: int,
) -> tuple[str, dict[str, Any]]:
    site_occupancy, group_cells, group_sites, missing_cells = _site_occupancy(design)
    drawable_sites = [
        site
        for site in design.sites.values()
        if site.resource != "other" or site.name in site_occupancy
    ]
    if not drawable_sites:
        raise XdbError(f"no drawable FPGA resources found in checkpoint: {design.source}")

    # Auto-crop: use the extent of OCCUPIED sites (not all drawable sites) so
    # empty tile columns/rows at the die edges don't inflate the plot area.
    # Fall back to the full drawable extent if the design is empty.
    occupied_sites = [
        site for site in drawable_sites if site.name in site_occupancy
    ]
    if occupied_sites:
        min_x = min(site.x for site in occupied_sites)
        max_x = max(site.x for site in occupied_sites)
        min_y = min(site.y for site in occupied_sites)
        max_y = max(site.y for site in occupied_sites)
        # Small margin (in tile units) so bboxes don't touch the plot edge.
        margin_x = int(max(4, (max_x - min_x) * 0.01))
        margin_y = int(max(4, (max_y - min_y) * 0.01))
        min_x = max(0, min_x - margin_x)
        max_x = max_x + margin_x
        min_y = max(0, min_y - margin_y)
        max_y = max_y + margin_y
    else:
        min_x = min(site.x for site in drawable_sites)
        max_x = max(site.x for site in drawable_sites)
        min_y = min(site.y for site in drawable_sites)
        max_y = max(site.y for site in drawable_sites)
    raw_width = max(1.0, float(max_x - min_x))
    raw_height = max(1.0, float(max_y - min_y))
    # Vertical layout: die is drawn full-width and the legend stacks BELOW.
    # Fit the die to a fixed target width; height follows aspect ratio.
    target_plot_width = 1050.0
    scale = target_plot_width / raw_width
    plot_width  = raw_width  * scale
    plot_height = raw_height * scale
    left = 20.0
    top = 20.0
    legend_gap = 0.0
    legend_width = plot_width
    document_width = max(_MIN_DOCUMENT_WIDTH, left * 2 + plot_width)

    groups = sorted(group for group, count in group_sites.items() if count)
    if len(groups) > max_groups:
        raise XdbError(
            f"hierarchy depth {hierarchy_depth} produced {len(groups)} color groups; "
            f"lower --hierarchy-depth or increase --max-groups (current: {max_groups})"
        )
    resource_counts = Counter(site.resource for site in design.sites.values())
    occupied_resource_counts = Counter(design.sites[name].resource for name in site_occupancy)
    resource_keys = [
        key
        for key in _RESOURCE_ORDER
        if key != "other" and (resource_counts[key] or occupied_resource_counts[key])
    ]
    if occupied_resource_counts["other"]:
        resource_keys.append("other")
    metadata_resource_keys = [
        key for key in _RESOURCE_ORDER if resource_counts[key] or occupied_resource_counts[key]
    ]

    requested_pblocks = list(design.pblocks) if show_pblocks else []
    pblock_regions: list[tuple[FloorplanPblock, list[tuple[float, float, float, float]], str]] = []
    for index, pblock in enumerate(requested_pblocks):
        regions = _merge_pblock_rectangles(_pblock_raw_rectangles(design, pblock))
        if not regions:
            continue
        stroke = _group_color(index + len(groups))
        pblock_regions.append((pblock, regions, stroke))

    # Vertical layout: the resource legend and the placed-hierarchy legend
    # sit BELOW the plot. The resource legend uses multiple columns so it
    # doesn't stretch the whole figure vertically.
    # No caption, no legend — plot fills the entire document.
    main_height = plot_height + 16.0
    resource_legend_top = top + main_height   # dummies kept for downstream refs
    hierarchy_top = resource_legend_top
    document_height = top + main_height + top   # symmetric padding
    output_width = 1200
    output_height = max(600, round(output_width * document_height / document_width))

    def transform(site: FloorplanSite) -> tuple[float, float]:
        return (
            left + (site.x - min_x) * scale,
            top + (max_y - site.y) * scale,
        )

    background_points: dict[str, set[tuple[float, float]]] = defaultdict(set)
    occupied_rectangles: dict[tuple[str, str], list[tuple[float, float, float, float]]] = (
        defaultdict(list)
    )
    mixed_sites = 0
    # The auto-crop above trims (min_x, max_x, min_y, max_y) to the extent of
    # OCCUPIED sites, but drawable_sites still spans the whole die. Drawing the
    # unfiltered background layer would map tiles outside the crop to negative
    # SVG offsets (past the left/top border) or beyond plot_width/plot_height
    # (past the right/bottom border), so the die appears to spill outside the
    # plot box. Filter to the same cropped extent so the background stays inside.
    for site in drawable_sites:
        if site.x < min_x or site.x > max_x or site.y < min_y or site.y > max_y:
            continue
        point = transform(site)
        background_points[site.resource].add(point)
        counts = site_occupancy.get(site.name)
        if not counts:
            continue
        groups_at_site = sorted(counts)
        if len(groups_at_site) > 1:
            mixed_sites += 1
        base_width, base_height = _MARK_SIZE[site.resource]
        width = max(1.8, base_width)
        height = max(1.8, base_height)
        segment_width = width / len(groups_at_site)
        x = point[0] - width / 2.0
        y = point[1] - height / 2.0
        for index, group in enumerate(groups_at_site):
            current_width = (
                width - segment_width * index if index == len(groups_at_site) - 1 else segment_width
            )
            occupied_rectangles[(group, site.resource)].append(
                (x + segment_width * index, y, current_width, height)
            )

    color_by_group = {group: _group_color(index) for index, group in enumerate(groups)}

    # Bounding box per hierarchy group in SVG coordinates. Skip the trivial
    # "<top>" group so it doesn't wrap the whole die.
    group_bboxes: dict[str, tuple[float, float, float, float]] = {}
    for (group, _resource), rects in occupied_rectangles.items():
        if not rects:
            continue
        gx0 = min(r[0] for r in rects)
        gx1 = max(r[0] + r[2] for r in rects)
        gy0 = min(r[1] for r in rects)
        gy1 = max(r[1] + r[3] for r in rects)
        if group in group_bboxes:
            cx0, cy0, cx1, cy1 = group_bboxes[group]
            group_bboxes[group] = (min(cx0, gx0), min(cy0, gy0), max(cx1, gx1), max(cy1, gy1))
        else:
            group_bboxes[group] = (gx0, gy0, gx1, gy1)
    display_title = title or f"FPGA placement — {design.design or design.source.stem}"
    subtitle_parts = [
        part for part in (design.device, f"hierarchy depth {hierarchy_depth}") if part
    ]
    subtitle = " · ".join(subtitle_parts)

    metadata = {
        "schema": _SCHEMA,
        "checkpoint": design.source.name,
        "checkpoint_sha256": checkpoint_sha256,
        "design": design.design,
        "device": design.device,
        "vivado_version": design.tool_version,
        "hierarchy_depth": hierarchy_depth,
        "max_groups": max_groups,
        "primitive_cells": design.stats.get("primitive_cells"),
        "placed_cells": design.stats.get("placed_cells"),
        "unplaced_cells": design.stats.get("unplaced_cells"),
        "routing_errors": design.stats.get("routing_errors"),
        "rendered_cells": sum(group_cells.values()),
        "unmapped_placed_cells": missing_cells,
        "occupied_sites": len(site_occupancy),
        "mixed_hierarchy_sites": mixed_sites,
        "groups": [
            {
                "name": group,
                "color": color_by_group[group],
                "cells": group_cells[group],
                "occupied_sites": group_sites[group],
            }
            for group in groups
        ],
        "resources": {
            key: {
                "sites": resource_counts[key],
                "occupied_sites": occupied_resource_counts[key],
            }
            for key in metadata_resource_keys
        },
        "pblocks": [item.name for item in requested_pblocks],
        "rendered_pblocks": [item.name for item, _regions, _stroke in pblock_regions],
    }

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{output_width}" '
            f'height="{output_height}" viewBox="0 0 {_fmt(document_width)} '
            f'{_fmt(document_height)}" role="img">'
        ),
        f"  <title>{xml_escape(display_title)}</title>",
        (
            "  <desc>Physical FPGA resources from a routed Vivado checkpoint; "
            "occupied sites are colored by hierarchy.</desc>"
        ),
        (
            "  <metadata>"
            f"{xml_escape(json.dumps(metadata, sort_keys=True, separators=(',', ':')))}"
            "</metadata>"
        ),
        "  <style>",
        f"    text {{ font-family: Inter, 'DejaVu Sans', sans-serif; fill: {_THEME['ink']}; }}",
        "    .title { font-size: 26px; font-weight: 700; }",
        f"    .subtitle {{ font-size: 14px; fill: {_THEME['ink_muted']}; }}",
        "    .legend-title { font-size: 16px; font-weight: 700; }",
        "    .legend-label { font-size: 13px; }",
        "    .hierarchy-name { font-size: 13px; font-weight: 600; }",
        f"    .legend-detail {{ font-size: 12px; fill: {_THEME['ink_dim']}; }}",
        # (.pblock-label CSS removed — dashed pblock-outline overlay no longer emitted)
        "  </style>",
        f'  <rect width="{_fmt(document_width)}" height="{_fmt(document_height)}" fill="{_THEME["document_bg"]}"/>',
        (
            f'  <rect x="{_fmt(left - 8)}" y="{_fmt(top - 8)}" '
            f'width="{_fmt(plot_width + 16)}" height="{_fmt(plot_height + 16)}" '
            f'rx="4" fill="{_THEME["plot_bg"]}" stroke="{_THEME["plot_stroke"]}" stroke-width="1"/>'
        ),
        # (device-resources background layer intentionally omitted — keeps
        # the plot uncluttered; a subtle unified CLB shading is drawn below.)
    ]

    # Subtle single-hue background covering ALL device sites, so the die
    # geometry (I/O banks, GT bricks, gaps) is visible without a rainbow of
    # resource-type colors.
    lines.append('  <g id="device-canvas">')
    all_points: set[tuple[float, float]] = set()
    for rset in background_points.values():
        all_points.update(rset)
    width, height = _MARK_SIZE["logic"]
    for path, _count in _mark_path_chunks(all_points, width, height):
        lines.append(
            f'    <path d="{path}" fill="#3a3f47" opacity="0.55"/>'
        )
    lines.append("  </g>")

    # Distinct pass for hard-IP tiles (GTY/CMAC/HBM/PCIe/NOC) so the reader
    # can see WHERE user code is off-limits — this is what makes the leftmost
    # vFPGA-carveouts (pblocks 4/5/6) legible instead of unexplained gaps.
    # Uses the palette's own transceiver/hard colors at higher opacity so
    # they stand out against the muted grey background.
    lines.append('  <g id="hard-ip-tiles">')
    for resource_key, palette_key in (("transceiver", "transceiver"), ("hard", "hard")):
        pts = background_points.get(resource_key)
        if not pts:
            continue
        mw, mh = _MARK_SIZE[resource_key]
        fill = _RESOURCE_COLORS[palette_key]
        for path, _count in _mark_path_chunks(pts, mw, mh):
            lines.append(
                f'    <path d="{path}" fill="{fill}" opacity="0.9"/>'
            )
    lines.append("  </g>")

    lines.append('  <g id="placed-hierarchies">')
    for group in groups:
        for resource in _RESOURCE_ORDER:
            rectangles = occupied_rectangles.get((group, resource), [])
            if not rectangles:
                continue
            for path, site_count in _rectangle_path_chunks(rectangles):
                lines.extend(
                    [
                        (
                            f'    <path d="{path}" fill="{color_by_group[group]}" '
                            f'opacity="0.94" data-hierarchy={quoteattr(group)} '
                            f'data-resource="{resource}">'
                        ),
                        (
                            f"      <title>{xml_escape(group)} — {_RESOURCE_LABELS[resource]}: "
                            f"{site_count} occupied sites</title>"
                        ),
                        "    </path>",
                    ]
                )
    lines.append("  </g>")

    # Outlined bounding boxes + inline labels for the vFPGA wrappers.
    # Prefer the XDC-declared pblock bounds for each inst_user_wrapper_N over
    # the cell-derived bbox: with strict-pblock DFX, ~99% of cells end up inside
    # the pblock, but phys_opt replicas / global-buffer neighbors / stray
    # LUTRAM cells outside stretch the raw bbox across nearly the whole die
    # and hide the physical isolation the paper is trying to show. Fall back
    # to the cell-derived bbox only if the wrapper has no matching pblock.
    import re as _re
    _INTERESTING_RE = _re.compile(r"^inst_user_wrapper_(\d+)$")
    _PBLOCK_RE      = _re.compile(r"^pblock_inst_user_wrapper_(\d+)$")
    lines.append('  <g id="hierarchy-bboxes">')

    # Use ONLY the pblock's SLICE-derived rectangles for the outline. URAM/BRAM/
    # DSP ranges snap to slightly different tile ROWs than SLICE, so mixing all
    # resource rects makes adjacent bands overlap 15-20% even though their
    # SLICE bands don't. The SLICE rectangle is what a paper reader reads as
    # "the vFPGA's physical region."
    pblock_bbox_by_idx: dict[int, tuple[float, float, float, float]] = {}
    for pblock, _raw_regions, _stroke in pblock_regions:
        m = _PBLOCK_RE.match(pblock.name)
        if not m:
            continue
        slice_rects_raw = [
            item for item in pblock.ranges
            if ":" in item and item.startswith("SLICE_")
        ]
        transformed: list[tuple[float, float, float, float]] = []
        for item in slice_rects_raw:
            first_name, second_name = item.split(":", 1)
            first = design.sites.get(first_name)
            second = design.sites.get(second_name)
            if first is None or second is None:
                continue
            rx0 = float(min(first.x, second.x))
            ry0 = float(min(first.y, second.y))
            rx1 = float(max(first.x, second.x))
            ry1 = float(max(first.y, second.y))
            sx0 = left + (rx0 - min_x) * scale
            sx1 = left + (rx1 - min_x) * scale
            # Y is inverted: max_y - device_y
            sy0 = top + (max_y - ry1) * scale
            sy1 = top + (max_y - ry0) * scale
            transformed.append((sx0, sy0, sx1, sy1))
        if not transformed:
            continue
        gx0 = min(r[0] for r in transformed)
        gy0 = min(r[1] for r in transformed)
        gx1 = max(r[2] for r in transformed)
        gy1 = max(r[3] for r in transformed)
        pblock_bbox_by_idx[int(m.group(1))] = (gx0, gy0, gx1, gy1)

    # First pass: compute bboxes for all vFPGA groups, sorted numerically.
    vfpga_entries = []
    for group, (gx0, gy0, gx1, gy1) in group_bboxes.items():
        m = _INTERESTING_RE.match(group)
        if not m:
            continue
        idx = int(m.group(1))
        pb = pblock_bbox_by_idx.get(idx)
        if pb is not None:
            gx0, gy0, gx1, gy1 = pb
        vfpga_entries.append((idx, group, gx0, gy0, gx1, gy1))
    vfpga_entries.sort()

    # Colored bbox OUTLINES per vFPGA — drawn first so labels sit on top. No
    # pad: adjacent pblocks share exact SLICE_Y boundaries and any pad would
    # introduce a fake ~8px overlap at every band boundary.
    def _draw_outline(x0: float, y0: float, x1: float, y1: float, color: str, tooltip: str) -> None:
        lines.append(
            f'    <rect x="{_fmt(x0)}" y="{_fmt(y0)}" '
            f'width="{_fmt(x1 - x0)}" height="{_fmt(y1 - y0)}" '
            f'fill="none" stroke="{color}" stroke-width="4.5" opacity="0.95" rx="3">'
            f'<title>{xml_escape(tooltip)}</title></rect>'
        )

    for _idx, group, gx0, gy0, gx1, gy1 in vfpga_entries:
        _draw_outline(gx0, gy0, gx1, gy1, color_by_group[group], group)

    # Additional outlines for shell / static / hard-IP regions — same visual
    # style as vFPGA outlines, so the reader gets a labeled box for every
    # region on the die instead of a separate top-right legend.
    LABEL_H = 32.0
    LABEL_FONT_SIZE = 18
    LABEL_CHAR_W = 11.0
    LABEL_PAD_X = 10.0
    placed_labels: list[tuple[float, float, float, float]] = []
    label_entries: list[tuple[str, str, float, float]] = []  # (color, label, lx, ly)

    for idx, group, gx0, gy0, _gx1, _gy1 in vfpga_entries:
        label_entries.append((color_by_group[group], f"vFPGA {idx}", gx0, gy0))

    # 2D gap-based clustering: split rects (or points-as-tiny-rects) into
    # spatially disjoint groups. Two rects merge if the distance between
    # their AABBs is ≤ gap_x on X *and* ≤ gap_y on Y. Separate axis gaps
    # matter for hard-IP tiles that form long horizontal rows (HBM/PCIe/
    # CMAC along the bottom of U280) — a big gap_x bridges the row while a
    # small gap_y keeps the row separate from mid-column CIPS/CONFIG tiles.
    # Simple union-find pass — O(N^2), fine for the O(100) rects we get.
    def _cluster_rects(
        rects: list[tuple[float, float, float, float]],
        gap: float | None = None,
        gap_x: float | None = None,
        gap_y: float | None = None,
    ) -> list[tuple[float, float, float, float]]:
        if gap_x is None:
            gap_x = gap if gap is not None else 0.0
        if gap_y is None:
            gap_y = gap if gap is not None else 0.0
        if not rects:
            return []
        n = len(rects)
        parent = list(range(n))
        def _find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        def _union(i: int, j: int) -> None:
            ri, rj = _find(i), _find(j)
            if ri != rj:
                parent[ri] = rj
        for i in range(n):
            ix0, iy0, ix1, iy1 = rects[i]
            for j in range(i + 1, n):
                jx0, jy0, jx1, jy1 = rects[j]
                dx = max(0.0, max(ix0 - jx1, jx0 - ix1))
                dy = max(0.0, max(iy0 - jy1, jy0 - iy1))
                if dx <= gap_x and dy <= gap_y:
                    _union(i, j)
        groups: dict[int, list[int]] = {}
        for i in range(n):
            groups.setdefault(_find(i), []).append(i)
        unions: list[tuple[float, float, float, float, int]] = []
        for members in groups.values():
            xs0 = [rects[i][0] for i in members]
            ys0 = [rects[i][1] for i in members]
            xs1 = [rects[i][2] for i in members]
            ys1 = [rects[i][3] for i in members]
            unions.append((min(xs0), min(ys0), max(xs1), max(ys1), len(members)))
        # Strip the member-count from the return type to keep the public
        # signature stable; the count is used only inside this function for
        # optional filtering by callers who care about cluster size.
        return [(a, b, c, d) for a, b, c, d, _n in unions]

    def _cluster_rects_sized(
        rects: list[tuple[float, float, float, float]],
        gap_x: float,
        gap_y: float,
    ) -> list[tuple[float, float, float, float, int]]:
        """Same as _cluster_rects but returns cluster member count too."""
        if not rects:
            return []
        n = len(rects)
        parent = list(range(n))
        def _find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for i in range(n):
            ix0, iy0, ix1, iy1 = rects[i]
            for j in range(i + 1, n):
                jx0, jy0, jx1, jy1 = rects[j]
                dx = max(0.0, max(ix0 - jx1, jx0 - ix1))
                dy = max(0.0, max(iy0 - jy1, jy0 - iy1))
                if dx <= gap_x and dy <= gap_y:
                    ri, rj = _find(i), _find(j)
                    if ri != rj:
                        parent[ri] = rj
        groups: dict[int, list[int]] = {}
        for i in range(n):
            groups.setdefault(_find(i), []).append(i)
        out: list[tuple[float, float, float, float, int]] = []
        for members in groups.values():
            xs0 = [rects[i][0] for i in members]
            ys0 = [rects[i][1] for i in members]
            xs1 = [rects[i][2] for i in members]
            ys1 = [rects[i][3] for i in members]
            out.append((min(xs0), min(ys0), max(xs1), max(ys1), len(members)))
        return out

    def _label_pos_for(
        bboxes: list[tuple[float, float, float, float]],
        prefer: str = "largest",
    ) -> tuple[float, float] | None:
        """Return an anchor point (top-left) for a label chip.

        prefer='largest'   → largest bbox (by area)
        prefer='leftmost'  → bbox containing the smallest x (for GTY on U280,
                             where QSFP CMAC lives at the left edge and any
                             mystery right-side GTY sites are not what a
                             reader means by "CMAC's transceivers")
        """
        if not bboxes:
            return None
        if prefer == "leftmost":
            chosen = min(bboxes, key=lambda b: b[0])
        else:
            chosen = max(bboxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        return (chosen[0], chosen[1])

    # -- Shell: pblock_inst_shell tessellates each SLR-strip into many small
    # rects; merge adjacent ones so we draw one clean outline per SLR strip.
    shell_color = color_by_group.get("inst_shell")
    if shell_color is not None:
        shell_svg_rects: list[tuple[float, float, float, float]] = []
        for pblock, raw_regions, _ in pblock_regions:
            if pblock.name != "pblock_inst_shell":
                continue
            for rx0, ry0, rx1, ry1 in raw_regions:
                sx0 = left + (rx0 - min_x) * scale
                sx1 = left + (rx1 - min_x) * scale
                sy0 = top + (max_y - ry1) * scale
                sy1 = top + (max_y - ry0) * scale
                shell_svg_rects.append((sx0, sy0, sx1, sy1))
        # 8px gap ≈ 2 tile widths — merges strips within the same SLR band
        # but keeps the top/mid/bot SLR strips separate.
        shell_unions = _cluster_rects(shell_svg_rects, gap=8.0)
        for sx0, sy0, sx1, sy1 in shell_unions:
            _draw_outline(sx0, sy0, sx1, sy1, shell_color, "inst_shell")
        pos = _label_pos_for(shell_unions)
        if pos is not None:
            label_entries.append((shell_color, "shell", pos[0], pos[1]))

    # -- Static: draw a DASHED outline (not solid like vFPGA/shell). Static
    # is the pre-routed wrapper that CONTAINS shell + user regions, so a
    # solid box would visually swallow the shell strips inside it. A dashed
    # outline reads as "containing envelope" and stays distinguishable.
    static_color = color_by_group.get("inst_static")
    if static_color is not None and "inst_static" in group_bboxes:
        gx0, gy0, gx1, gy1 = group_bboxes["inst_static"]
        lines.append(
            f'    <rect x="{_fmt(gx0)}" y="{_fmt(gy0)}" '
            f'width="{_fmt(gx1 - gx0)}" height="{_fmt(gy1 - gy0)}" '
            f'fill="none" stroke="{static_color}" stroke-width="3" '
            f'stroke-dasharray="10 6" opacity="0.9" rx="3">'
            f'<title>inst_static</title></rect>'
        )
        label_entries.append((static_color, "static", gx0, gy0))

    # -- Hard-IP regions: no outline (they're scattered — HBM row + PCIe
    # corner + NoC repeaters spread across the die, and any bbox is either
    # a swarm of tiny per-tile rects or one mega-rect that misleads about
    # ownership). The `hard-ip-tiles` background layer already shows the
    # exact positions in olive/slate; just add a legend chip anchored to
    # the largest tile cluster so the reader can bind color → name.
    # Hard IP outlines: only the `hard` bucket gets outlined + labeled
    # (covers PCIe + HBM + CMAC + ILKN + SYSMON + CONFIG). Big gap_x bridges
    # the long HBM/CMAC row along the bottom of the die into one clean box;
    # small gap_y keeps that row separate from mid-column CIPS/CONFIG tiles.
    # Clusters with fewer than 5 tiles are dropped as noise (isolated
    # CONFIG_SITE / SYSMONE4 singletons that don't merit their own outline).
    # Transceivers (GTY) have their fill but no outline/label: the QSFP GT
    # column is a single skinny vertical strip and its label just floats.
    for res_key, res_label, anchor_pref in (
        ("hard",        "hard IP", "largest"),
    ):
        pts = background_points.get(res_key, set())
        if not pts:
            continue
        mw, mh = _MARK_SIZE[res_key]
        hw = mw / 2.0
        hh = mh / 2.0
        tile_rects = [(p[0] - hw, p[1] - hh, p[0] + hw, p[1] + hh) for p in pts]
        # Axis-aware gaps: large X to merge the long HBM/CMAC row across the
        # die width, small Y to keep it separate from taller vertical strips.
        sized_clusters = _cluster_rects_sized(tile_rects, gap_x=200.0, gap_y=25.0)
        # Keep only significant clusters (≥5 tiles) so we don't outline every
        # stray SYSMON/CONFIG singleton.
        big_clusters = [c for c in sized_clusters if c[4] >= 5]
        color = _RESOURCE_COLORS[res_key]
        # HBM/CMAC/PCIe tiles form a single-tile-tall row along the die's
        # bottom, giving a bbox only ~5 SVG px tall — invisible as a stroked
        # outline. Pad each hard-IP cluster by 8 px in Y (a bit less in X)
        # and clamp to the plot area so it reads as a proper region.
        pad_x = 4.0
        pad_y = 8.0
        plot_x0, plot_y0 = left, top
        plot_x1, plot_y1 = left + plot_width, top + plot_height
        padded_bboxes: list[tuple[float, float, float, float]] = []
        for bx0, by0, bx1, by1, _n in big_clusters:
            px0 = max(plot_x0, bx0 - pad_x)
            py0 = max(plot_y0, by0 - pad_y)
            px1 = min(plot_x1, bx1 + pad_x)
            py1 = min(plot_y1, by1 + pad_y)
            padded_bboxes.append((px0, py0, px1, py1))
            _draw_outline(px0, py0, px1, py1, color, res_label)
        pos = _label_pos_for(padded_bboxes, prefer=anchor_pref)
        if pos is not None:
            label_entries.append((color, res_label, pos[0], pos[1]))

    plot_right_edge = left + plot_width
    for color, label, lx, ly in label_entries:
        text_w = LABEL_CHAR_W * len(label) + 2 * LABEL_PAD_X
        # Clamp the chip so it doesn't overflow the plot rectangle on the
        # right — anchor-at-bbox-top-left plus a wide chip could otherwise
        # push the chip past plot_right (visible as a clipped label).
        if lx + text_w > plot_right_edge:
            lx = plot_right_edge - text_w
        collides = True
        while collides:
            collides = False
            for px0, py0, pw, ph in placed_labels:
                if not (lx + text_w < px0 or px0 + pw < lx or
                        ly + LABEL_H < py0 or py0 + ph < ly):
                    ly = py0 + ph + 4.0
                    collides = True
                    break
        placed_labels.append((lx, ly, text_w, LABEL_H))
        # Pick text color by chip-color luminance so labels on dark chips
        # (e.g. shell #5a5040) don't become invisible near-black-on-near-black.
        _hex = color.lstrip("#")
        _r, _g, _b = int(_hex[0:2], 16), int(_hex[2:4], 16), int(_hex[4:6], 16)
        _lum = 0.299 * _r + 0.587 * _g + 0.114 * _b
        text_fill = "#111111" if _lum > 140 else "#ffffff"
        lines.append(
            f'    <rect x="{_fmt(lx)}" y="{_fmt(ly)}" width="{_fmt(text_w)}" '
            f'height="{_fmt(LABEL_H)}" fill="{color}" opacity="0.96" rx="4" '
            f'stroke="#111111" stroke-width="1.2"/>'
        )
        lines.append(
            f'    <text x="{_fmt(lx + LABEL_PAD_X)}" '
            f'y="{_fmt(ly + LABEL_H - 10)}" '
            f'font-family="Inter, DejaVu Sans, sans-serif" '
            f'font-size="{LABEL_FONT_SIZE}" font-weight="700" fill="{text_fill}">'
            f'{xml_escape(label)}</text>'
        )
    lines.append("  </g>")

    # (Removed: dashed pblock-outline overlay + pblock_inst_* labels. The vFPGA
    # colored bboxes above already convey the isolation regions; drawing another
    # pass of dashed rects with pblock_inst_user_wrapper_N text made the figure
    # noisy without adding information.)

    lines.extend(
        [
            # legend + caption intentionally omitted — plot only.
            "  <g id=\"device-legend\"></g>",
            '  <g id="hierarchy-legend"></g>',
        ]
    )

    # (external legend suppressed; labels are drawn inline on the bboxes.)

    lines.extend(["</svg>", ""])
    return "\n".join(lines), metadata


def render_floorplan_svg(
    design: FloorplanDesign,
    *,
    title: str | None = None,
    hierarchy_depth: int = 1,
    checkpoint_sha256: str | None = None,
    show_pblocks: bool = True,
    max_groups: int = 32,
) -> tuple[str, dict[str, Any]]:
    if hierarchy_depth <= 0:
        raise XdbError("hierarchy depth must be > 0")
    if max_groups <= 0:
        raise XdbError("max groups must be > 0")
    _validate_title(title)
    digest = checkpoint_sha256 or _sha256(design.source)
    return _svg_document(
        design,
        title=title,
        hierarchy_depth=hierarchy_depth,
        checkpoint_sha256=digest,
        show_pblocks=show_pblocks,
        max_groups=max_groups,
    )


def _validate_svg_output(path: Path, *, force: bool) -> None:
    if path.suffix.lower() != ".svg":
        raise XdbError(f"floorplan output must use the .svg extension: {path}")
    if os.path.lexists(path):
        if not force:
            raise XdbError(f"output already exists (pass --force to replace it): {path}")
        if path.is_dir():
            raise XdbError(f"floorplan output is a directory: {path}")

    ancestor = path.parent
    while not os.path.lexists(ancestor):
        if ancestor == ancestor.parent:
            break
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise XdbError(f"output parent is not a directory: {ancestor}")
    if not os.access(ancestor, os.W_OK | os.X_OK):
        raise XdbError(f"output parent is not writable: {ancestor}")


def _default_output_mode() -> int:
    current_umask = os.umask(0)
    os.umask(current_umask)
    return 0o666 & ~current_umask


def _write_svg(path: Path, svg: str, *, force: bool) -> None:
    _validate_svg_output(path, force=force)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise XdbError(f"failed to create output directory: {path.parent}") from error

    output_mode = _default_output_mode()
    if force and os.path.lexists(path):
        try:
            existing_mode = os.lstat(path).st_mode
            if stat.S_ISREG(existing_mode):
                output_mode = stat.S_IMODE(existing_mode)
        except OSError:
            pass

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(svg)
        os.chmod(temporary, output_mode)
        if force:
            os.replace(temporary, path)
            temporary = None
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as error:
                raise XdbError(
                    f"output already exists (pass --force to replace it): {path}"
                ) from error
            temporary.unlink()
            temporary = None
    except XdbError:
        raise
    except OSError as error:
        raise XdbError(f"failed to write floorplan SVG: {path}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def generate_floorplan_svg(
    path: str | Path,
    *,
    output: str | Path,
    dcp: str | Path | None = None,
    hierarchy_depth: int = 1,
    title: str | None = None,
    show_pblocks: bool = True,
    max_groups: int = 32,
    force: bool = False,
    timeout: int = 1800,
) -> dict[str, Any]:
    if max_groups <= 0:
        raise XdbError("--max-groups must be > 0")
    _validate_title(title)
    output_path = Path(output).expanduser()
    _validate_svg_output(output_path, force=force)
    checkpoint = discover_floorplan_checkpoint(path, dcp=dcp)
    digest = _sha256(checkpoint)
    design = inspect_floorplan_checkpoint(
        checkpoint,
        hierarchy_depth=hierarchy_depth,
        timeout=timeout,
    )
    if _sha256(checkpoint) != digest:
        raise XdbError(f"checkpoint changed while Vivado was inspecting it: {checkpoint}")
    svg, metadata = render_floorplan_svg(
        design,
        title=title,
        hierarchy_depth=hierarchy_depth,
        checkpoint_sha256=digest,
        show_pblocks=show_pblocks,
        max_groups=max_groups,
    )
    _write_svg(output_path, svg, force=force)
    return {
        **metadata,
        "source": str(checkpoint),
        "output": str(output_path),
    }


def format_floorplan_report(data: dict[str, Any]) -> str:
    groups = data.get("groups")
    group_count = len(groups) if isinstance(groups, list) else 0
    lines = [
        f"Floorplan SVG: {data.get('output')}",
        f"Checkpoint: {data.get('source')}",
        f"Device: {data.get('device') or 'unknown'}",
        (
            f"Placement: {data.get('placed_cells') or 0:,} placed primitives, "
            f"{data.get('occupied_sites') or 0:,} occupied sites, {group_count} hierarchy groups"
        ),
    ]
    unmapped = data.get("unmapped_placed_cells")
    if isinstance(unmapped, int) and unmapped:
        lines.append(
            f"Warning: {unmapped:,} placed primitives used sites without render coordinates"
        )
    return "\n".join(lines)
