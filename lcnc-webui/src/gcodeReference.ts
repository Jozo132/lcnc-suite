// ─── LinuxCNC G-code & M-code Reference ─────────────────────────
//
// Static lookup table for all G/M codes supported by LinuxCNC.
// Data source: https://linuxcnc.org/docs/html/gcode/g-code.html
//              https://linuxcnc.org/docs/html/gcode/m-code.html
//              https://linuxcnc.org/docs/html/gcode/other-code.html

export interface GcodeEntry {
  code: string;
  name: string;
  desc: string;
  syntax: string;
  group: string;
}

export const GCODE_GROUPS = [
  "Motion",
  "Compensation",
  "Coordinate System",
  "Canned Cycles",
  "Distance & Feed",
  "Spindle & Coolant",
  "Stopping",
  "I/O & Flow",
  "Other",
] as const;

export const GCODE_REFERENCE: GcodeEntry[] = [
  // ─── Motion ────────────────────────────────────────────
  { code: "G0", name: "Rapid Move", desc: "Move at maximum traverse rate. Not coordinated — each axis moves at its own max speed.", syntax: "G0 X.. Y.. Z.. A.. B.. C..", group: "Motion" },
  { code: "G1", name: "Linear Move", desc: "Coordinated linear move at the current feed rate (F word).", syntax: "G1 X.. Y.. Z.. A.. B.. C.. F..", group: "Motion" },
  { code: "G2", name: "Arc CW", desc: "Clockwise circular arc. Center specified by I/J/K (incremental from start) or R (radius).", syntax: "G2 X.. Y.. I.. J.. F.. | G2 X.. Y.. R.. F..", group: "Motion" },
  { code: "G3", name: "Arc CCW", desc: "Counter-clockwise circular arc. Same parameters as G2.", syntax: "G3 X.. Y.. I.. J.. F.. | G3 X.. Y.. R.. F..", group: "Motion" },
  { code: "G4", name: "Dwell", desc: "Pause for the specified time. P is in seconds.", syntax: "G4 P..", group: "Motion" },
  { code: "G5", name: "Cubic Spline", desc: "Cubic B-spline with control points. I/J = first control point offsets, P/Q = second.", syntax: "G5 X.. Y.. I.. J.. P.. Q..", group: "Motion" },
  { code: "G5.1", name: "Quadratic Spline", desc: "Quadratic B-spline. I/J = control point offsets from start.", syntax: "G5.1 X.. Y.. I.. J..", group: "Motion" },
  { code: "G5.2", name: "NURBS Start", desc: "Begin a NURBS (Non-Uniform Rational B-Spline) block. Followed by P/X/Y lines, closed by G5.3.", syntax: "G5.2 P.. X.. Y..", group: "Motion" },
  { code: "G5.3", name: "NURBS End", desc: "End a NURBS block started by G5.2.", syntax: "G5.3", group: "Motion" },
  { code: "G33", name: "Spindle Sync Move", desc: "Spindle-synchronized linear move for threading. K = distance per revolution.", syntax: "G33 X.. Y.. Z.. K..", group: "Motion" },
  { code: "G33.1", name: "Rigid Tapping", desc: "Rigid tapping cycle. Spindle and Z axis move in sync. K = distance per revolution.", syntax: "G33.1 X.. Y.. Z.. K..", group: "Motion" },
  { code: "G38.2", name: "Probe Toward (error)", desc: "Probe toward workpiece — error if no contact. Stops on contact and records position.", syntax: "G38.2 X.. Y.. Z.. F..", group: "Motion" },
  { code: "G38.3", name: "Probe Toward (no error)", desc: "Probe toward workpiece — no error if no contact. Records position if contact made.", syntax: "G38.3 X.. Y.. Z.. F..", group: "Motion" },
  { code: "G38.4", name: "Probe Away (error)", desc: "Probe away from workpiece — error if no loss of contact.", syntax: "G38.4 X.. Y.. Z.. F..", group: "Motion" },
  { code: "G38.5", name: "Probe Away (no error)", desc: "Probe away from workpiece — no error if no loss of contact.", syntax: "G38.5 X.. Y.. Z.. F..", group: "Motion" },

  // ─── Compensation ──────────────────────────────────────
  { code: "G40", name: "Comp Off", desc: "Cancel cutter radius compensation.", syntax: "G40", group: "Compensation" },
  { code: "G41", name: "Comp Left", desc: "Cutter radius compensation left of programmed path. D word selects tool table entry.", syntax: "G41 D..", group: "Compensation" },
  { code: "G41.1", name: "Comp Left (dynamic)", desc: "Cutter compensation left with diameter specified directly (not from tool table).", syntax: "G41.1 D.. L..", group: "Compensation" },
  { code: "G42", name: "Comp Right", desc: "Cutter radius compensation right of programmed path.", syntax: "G42 D..", group: "Compensation" },
  { code: "G42.1", name: "Comp Right (dynamic)", desc: "Cutter compensation right with diameter specified directly.", syntax: "G42.1 D.. L..", group: "Compensation" },
  { code: "G43", name: "Tool Length Offset", desc: "Apply tool length offset from tool table. H word selects entry.", syntax: "G43 H..", group: "Compensation" },
  { code: "G43.1", name: "Tool Length (dynamic)", desc: "Apply tool length offset with values specified directly.", syntax: "G43.1 X.. Y.. Z..", group: "Compensation" },
  { code: "G43.2", name: "Tool Length (additive)", desc: "Apply additional tool length offset from a second tool entry.", syntax: "G43.2 H..", group: "Compensation" },
  { code: "G49", name: "Tool Length Cancel", desc: "Cancel tool length compensation.", syntax: "G49", group: "Compensation" },

  // ─── Coordinate System ─────────────────────────────────
  { code: "G10 L2", name: "Set WCS Origin", desc: "Set coordinate system origin. P selects system (1=G54..9=G59.3). Absolute values.", syntax: "G10 L2 P.. X.. Y.. Z..", group: "Coordinate System" },
  { code: "G10 L20", name: "Set WCS (current pos)", desc: "Set coordinate system so that the current position becomes the specified values.", syntax: "G10 L20 P.. X.. Y.. Z..", group: "Coordinate System" },
  { code: "G10 L1", name: "Set Tool Table", desc: "Set tool table entry. P = tool number. R = radius, Z = length offset.", syntax: "G10 L1 P.. R.. Z..", group: "Coordinate System" },
  { code: "G10 L10", name: "Set Tool (current pos)", desc: "Set tool offset so the current position becomes the given value.", syntax: "G10 L10 P.. Z..", group: "Coordinate System" },
  { code: "G10 L11", name: "Set Tool (workpiece)", desc: "Set tool offset computed from current position and workpiece coordinate.", syntax: "G10 L11 P.. Z..", group: "Coordinate System" },
  { code: "G28", name: "Go to Predefined", desc: "Make a rapid move to the predefined position (set by G28.1) via the intermediate point.", syntax: "G28 X.. Y.. Z..", group: "Coordinate System" },
  { code: "G28.1", name: "Set G28 Position", desc: "Store the current absolute position as the G28 predefined position.", syntax: "G28.1", group: "Coordinate System" },
  { code: "G30", name: "Go to Predefined 2", desc: "Make a rapid move to the second predefined position (set by G30.1).", syntax: "G30 X.. Y.. Z..", group: "Coordinate System" },
  { code: "G30.1", name: "Set G30 Position", desc: "Store the current absolute position as the G30 predefined position.", syntax: "G30.1", group: "Coordinate System" },
  { code: "G53", name: "Machine Coordinates", desc: "Move in machine (absolute) coordinate system. Non-modal — applies to this line only.", syntax: "G53 G0 X.. Y.. Z..", group: "Coordinate System" },
  { code: "G54", name: "WCS 1", desc: "Select work coordinate system 1 (default).", syntax: "G54", group: "Coordinate System" },
  { code: "G55", name: "WCS 2", desc: "Select work coordinate system 2.", syntax: "G55", group: "Coordinate System" },
  { code: "G56", name: "WCS 3", desc: "Select work coordinate system 3.", syntax: "G56", group: "Coordinate System" },
  { code: "G57", name: "WCS 4", desc: "Select work coordinate system 4.", syntax: "G57", group: "Coordinate System" },
  { code: "G58", name: "WCS 5", desc: "Select work coordinate system 5.", syntax: "G58", group: "Coordinate System" },
  { code: "G59", name: "WCS 6", desc: "Select work coordinate system 6.", syntax: "G59", group: "Coordinate System" },
  { code: "G59.1", name: "WCS 7", desc: "Select work coordinate system 7.", syntax: "G59.1", group: "Coordinate System" },
  { code: "G59.2", name: "WCS 8", desc: "Select work coordinate system 8.", syntax: "G59.2", group: "Coordinate System" },
  { code: "G59.3", name: "WCS 9", desc: "Select work coordinate system 9.", syntax: "G59.3", group: "Coordinate System" },
  { code: "G92", name: "Coordinate Offset", desc: "Make the current point have the given coordinates (temporary offset, not saved).", syntax: "G92 X.. Y.. Z..", group: "Coordinate System" },
  { code: "G92.1", name: "Reset G92 Offset", desc: "Clear G92 offset (set to zero) and reset from axis positions.", syntax: "G92.1", group: "Coordinate System" },
  { code: "G92.2", name: "Disable G92 Offset", desc: "Clear G92 offset without resetting. The offset is saved but not applied.", syntax: "G92.2", group: "Coordinate System" },
  { code: "G92.3", name: "Restore G92 Offset", desc: "Restore a previously saved G92 offset.", syntax: "G92.3", group: "Coordinate System" },

  // ─── Canned Cycles ─────────────────────────────────────
  { code: "G80", name: "Cancel Canned Cycle", desc: "Cancel any active canned cycle (G81-G89).", syntax: "G80", group: "Canned Cycles" },
  { code: "G81", name: "Drill", desc: "Simple drilling cycle. Rapid to R, feed to Z, rapid out.", syntax: "G81 X.. Y.. Z.. R.. F..", group: "Canned Cycles" },
  { code: "G82", name: "Drill with Dwell", desc: "Drilling with dwell at bottom. P = dwell time in seconds.", syntax: "G82 X.. Y.. Z.. R.. P.. F..", group: "Canned Cycles" },
  { code: "G83", name: "Peck Drill", desc: "Peck drilling — retracts to R between pecks to clear chips. Q = peck depth.", syntax: "G83 X.. Y.. Z.. R.. Q.. F..", group: "Canned Cycles" },
  { code: "G73", name: "Chip-Break Drill", desc: "High-speed peck drilling — retracts slightly between pecks (chip break, not full retract).", syntax: "G73 X.. Y.. Z.. R.. Q.. F..", group: "Canned Cycles" },
  { code: "G84", name: "Right-Hand Tap", desc: "Right-hand tapping cycle. Spindle reverses at bottom to retract.", syntax: "G84 X.. Y.. Z.. R.. F..", group: "Canned Cycles" },
  { code: "G85", name: "Bore (feed out)", desc: "Boring cycle — feeds in and feeds out at the same rate.", syntax: "G85 X.. Y.. Z.. R.. F..", group: "Canned Cycles" },
  { code: "G86", name: "Bore (stop, rapid out)", desc: "Boring cycle — spindle stops at bottom, then rapids out.", syntax: "G86 X.. Y.. Z.. R.. F..", group: "Canned Cycles" },
  { code: "G87", name: "Back Bore", desc: "Back boring cycle — enters from bottom, bores upward.", syntax: "G87 X.. Y.. Z.. R.. I.. J.. F..", group: "Canned Cycles" },
  { code: "G88", name: "Bore (dwell, manual out)", desc: "Boring with dwell at bottom. Manual retract (spindle stop + dwell, then retract).", syntax: "G88 X.. Y.. Z.. R.. P.. F..", group: "Canned Cycles" },
  { code: "G89", name: "Bore (dwell, feed out)", desc: "Boring with dwell at bottom, then feed out.", syntax: "G89 X.. Y.. Z.. R.. P.. F..", group: "Canned Cycles" },
  { code: "G76", name: "Thread Cycle", desc: "Multi-pass threading cycle. P = thread pitch, Z = final depth, I/J/K = first cut depth, taper, spring passes.", syntax: "G76 P.. Z.. I.. J.. K.. R.. Q.. H.. E.. L..", group: "Canned Cycles" },

  // ─── Distance & Feed ───────────────────────────────────
  { code: "G17", name: "XY Plane", desc: "Select XY plane for arcs (G2/G3) and canned cycles. Default plane.", syntax: "G17", group: "Distance & Feed" },
  { code: "G18", name: "XZ Plane", desc: "Select XZ plane for arcs and canned cycles (lathe threading).", syntax: "G18", group: "Distance & Feed" },
  { code: "G19", name: "YZ Plane", desc: "Select YZ plane for arcs and canned cycles.", syntax: "G19", group: "Distance & Feed" },
  { code: "G20", name: "Inch Mode", desc: "Interpret dimensions as inches.", syntax: "G20", group: "Distance & Feed" },
  { code: "G21", name: "Millimeter Mode", desc: "Interpret dimensions as millimeters.", syntax: "G21", group: "Distance & Feed" },
  { code: "G90", name: "Absolute Distance", desc: "Coordinates are absolute (relative to origin).", syntax: "G90", group: "Distance & Feed" },
  { code: "G91", name: "Incremental Distance", desc: "Coordinates are incremental (relative to current position).", syntax: "G91", group: "Distance & Feed" },
  { code: "G90.1", name: "Arc Absolute Mode", desc: "I/J/K arc center offsets are absolute.", syntax: "G90.1", group: "Distance & Feed" },
  { code: "G91.1", name: "Arc Incremental Mode", desc: "I/J/K arc center offsets are incremental (default).", syntax: "G91.1", group: "Distance & Feed" },
  { code: "G93", name: "Inverse Time Feed", desc: "Feed rate is in inverse time (1/minutes). F2 = move completes in 0.5 minutes.", syntax: "G93", group: "Distance & Feed" },
  { code: "G94", name: "Units per Minute", desc: "Feed rate in units (mm or inches) per minute. Default mode.", syntax: "G94", group: "Distance & Feed" },
  { code: "G95", name: "Units per Revolution", desc: "Feed rate in units per spindle revolution (requires spindle encoder).", syntax: "G95", group: "Distance & Feed" },
  { code: "G96", name: "Constant Surface Speed", desc: "Spindle speed is constant surface speed (CSS). S = surface speed, D = max RPM. Lathe mode.", syntax: "G96 S.. D..", group: "Distance & Feed" },
  { code: "G97", name: "RPM Mode", desc: "Spindle speed in RPM (cancel CSS). Default mode.", syntax: "G97", group: "Distance & Feed" },
  { code: "G61", name: "Exact Path Mode", desc: "Move exactly along programmed path. Machine decelerates at every corner.", syntax: "G61", group: "Distance & Feed" },
  { code: "G61.1", name: "Exact Stop Mode", desc: "Machine comes to exact stop at each programmed point.", syntax: "G61.1", group: "Distance & Feed" },
  { code: "G64", name: "Path Blending", desc: "Allow path blending for smoother motion. P = tolerance, Q = naive CAM tolerance.", syntax: "G64 P.. Q..", group: "Distance & Feed" },
  { code: "G98", name: "Canned: Initial Plane", desc: "Canned cycle retract to initial Z level (starting height).", syntax: "G98", group: "Distance & Feed" },
  { code: "G99", name: "Canned: R Plane", desc: "Canned cycle retract to R level.", syntax: "G99", group: "Distance & Feed" },

  // ─── Spindle & Coolant (M-codes) ───────────────────────
  { code: "M3", name: "Spindle CW", desc: "Start spindle clockwise (forward) at the programmed S speed.", syntax: "M3 S..", group: "Spindle & Coolant" },
  { code: "M4", name: "Spindle CCW", desc: "Start spindle counter-clockwise (reverse) at the programmed S speed.", syntax: "M4 S..", group: "Spindle & Coolant" },
  { code: "M5", name: "Spindle Stop", desc: "Stop the spindle.", syntax: "M5", group: "Spindle & Coolant" },
  { code: "M6", name: "Tool Change", desc: "Change to tool selected by T word. Executes the tool change procedure.", syntax: "T.. M6", group: "Spindle & Coolant" },
  { code: "M7", name: "Mist Coolant On", desc: "Turn on mist coolant.", syntax: "M7", group: "Spindle & Coolant" },
  { code: "M8", name: "Flood Coolant On", desc: "Turn on flood coolant.", syntax: "M8", group: "Spindle & Coolant" },
  { code: "M9", name: "Coolant Off", desc: "Turn off all coolant (both mist and flood).", syntax: "M9", group: "Spindle & Coolant" },
  { code: "M19", name: "Orient Spindle", desc: "Orient spindle to a specific angle. R = angle, Q = timeout.", syntax: "M19 R.. Q..", group: "Spindle & Coolant" },
  { code: "S", name: "Spindle Speed", desc: "Set spindle speed in RPM (or surface speed in G96 CSS mode).", syntax: "S..", group: "Spindle & Coolant" },
  { code: "T", name: "Select Tool", desc: "Select tool number for next M6 tool change. Does not change tool by itself.", syntax: "T..", group: "Spindle & Coolant" },
  { code: "F", name: "Feed Rate", desc: "Set feed rate in current units (mm/min or in/min in G94, units/rev in G95).", syntax: "F..", group: "Spindle & Coolant" },

  // ─── Stopping ──────────────────────────────────────────
  { code: "M0", name: "Program Pause", desc: "Pause program execution. Press cycle start to continue.", syntax: "M0", group: "Stopping" },
  { code: "M1", name: "Optional Stop", desc: "Pause if optional stop switch is on. Otherwise skipped.", syntax: "M1", group: "Stopping" },
  { code: "M2", name: "Program End", desc: "End program. Resets to default modal state.", syntax: "M2", group: "Stopping" },
  { code: "M30", name: "Program End + Rewind", desc: "End program and rewind to beginning. Same as M2 but resets line counter.", syntax: "M30", group: "Stopping" },
  { code: "M60", name: "Pallet Change + Pause", desc: "Pallet shuttle and program pause. Press cycle start to continue.", syntax: "M60", group: "Stopping" },

  // ─── I/O & Flow ────────────────────────────────────────
  { code: "M62", name: "Digital Out On (sync)", desc: "Turn on digital output synchronized with motion. P = output number.", syntax: "M62 P..", group: "I/O & Flow" },
  { code: "M63", name: "Digital Out Off (sync)", desc: "Turn off digital output synchronized with motion.", syntax: "M63 P..", group: "I/O & Flow" },
  { code: "M64", name: "Digital Out On (imm)", desc: "Turn on digital output immediately (not synced to motion).", syntax: "M64 P..", group: "I/O & Flow" },
  { code: "M65", name: "Digital Out Off (imm)", desc: "Turn off digital output immediately.", syntax: "M65 P..", group: "I/O & Flow" },
  { code: "M66", name: "Wait for Input", desc: "Wait for digital or analog input. P/E = input number, L = wait type, Q = timeout.", syntax: "M66 P.. L.. Q.. | M66 E.. L.. Q..", group: "I/O & Flow" },
  { code: "M67", name: "Analog Out (sync)", desc: "Set analog output synchronized with motion. E = output number, Q = value.", syntax: "M67 E.. Q..", group: "I/O & Flow" },
  { code: "M68", name: "Analog Out (imm)", desc: "Set analog output immediately.", syntax: "M68 E.. Q..", group: "I/O & Flow" },
  { code: "M100-M199", name: "User M-codes", desc: "User-defined M-codes. LinuxCNC runs the script M1xx from the [RS274NGC] USER_M_PATH.", syntax: "M1xx P.. Q..", group: "I/O & Flow" },
  { code: "O sub", name: "Subroutine Def", desc: "Define a named or numbered subroutine. Closed by O endsub.", syntax: "O<name> sub ... O<name> endsub", group: "I/O & Flow" },
  { code: "O call", name: "Subroutine Call", desc: "Call a subroutine by name or number. Parameters passed as positional args.", syntax: "O<name> call [arg1] [arg2] ..", group: "I/O & Flow" },
  { code: "O if/else", name: "Conditional", desc: "Conditional execution. Supports if/elseif/else/endif.", syntax: "O.. if [#cond] ... O.. else ... O.. endif", group: "I/O & Flow" },
  { code: "O while", name: "While Loop", desc: "Loop while condition is true.", syntax: "O.. while [#cond] ... O.. endwhile", group: "I/O & Flow" },
  { code: "O repeat", name: "Repeat Loop", desc: "Repeat block a fixed number of times.", syntax: "O.. repeat [#count] ... O.. endrepeat", group: "I/O & Flow" },

  // ─── Other ─────────────────────────────────────────────
  { code: "G52", name: "Local Offset", desc: "Apply a temporary local coordinate offset (added to active WCS). G52 X0 Y0 Z0 cancels.", syntax: "G52 X.. Y.. Z..", group: "Other" },
  { code: "G68", name: "Coordinate Rotation", desc: "Rotate coordinate system. R = angle in degrees around the current XY origin.", syntax: "G68 X.. Y.. R..", group: "Other" },
  { code: "G69", name: "Cancel Rotation", desc: "Cancel coordinate system rotation (G68).", syntax: "G69", group: "Other" },
  { code: "M48", name: "Override Enable", desc: "Enable feed and spindle speed override controls.", syntax: "M48", group: "Other" },
  { code: "M49", name: "Override Disable", desc: "Disable feed and spindle speed override controls.", syntax: "M49", group: "Other" },
  { code: "M50", name: "Feed Override", desc: "Enable (P1) or disable (P0) feed override.", syntax: "M50 P..", group: "Other" },
  { code: "M51", name: "Spindle Override", desc: "Enable (P1) or disable (P0) spindle speed override.", syntax: "M51 P..", group: "Other" },
  { code: "M52", name: "Adaptive Feed", desc: "Enable (P1) or disable (P0) adaptive feed. Requires motion.adaptive-feed HAL pin.", syntax: "M52 P..", group: "Other" },
  { code: "M53", name: "Feed Stop Control", desc: "Enable (P1) or disable (P0) feed stop switch. Requires motion.feed-hold HAL pin.", syntax: "M53 P..", group: "Other" },
  { code: "M61", name: "Set Tool Number", desc: "Change the current tool number without executing a tool change.", syntax: "M61 Q..", group: "Other" },
  { code: "M70", name: "Save Modal State", desc: "Save current modal state (G/M codes, feed, speed) to a stack for later restore.", syntax: "M70", group: "Other" },
  { code: "M71", name: "Invalidate State", desc: "Invalidate saved modal state (stored by M70) so M72 has no effect.", syntax: "M71", group: "Other" },
  { code: "M72", name: "Restore Modal State", desc: "Restore modal state previously saved by M70.", syntax: "M72", group: "Other" },
  { code: "M73", name: "Save + Autorestore", desc: "Save modal state and automatically restore at subroutine return (O endsub/return).", syntax: "M73", group: "Other" },
  { code: "#", name: "Parameters (vars)", desc: "Numbered (#1-#5399) and named (#<name>) variables. #1-#30 = subroutine args, #5061-#5069 = probe results.", syntax: "#.. = .. | #<name> = ..", group: "Other" },
  { code: "(MSG,..)", name: "Operator Message", desc: "Display a message to the operator. Text after MSG, is shown in the UI.", syntax: "(MSG, text here)", group: "Other" },
  { code: "(DEBUG,..)", name: "Debug Message", desc: "Print debug output. Can interpolate variables with #<name>.", syntax: "(DEBUG, var=#<_x>)", group: "Other" },
  { code: "(PRINT,..)", name: "Print to File", desc: "Print text to stderr (log file). Same syntax as DEBUG.", syntax: "(PRINT, text)", group: "Other" },
];

export const GCODE_LOOKUP: Map<string, GcodeEntry> = new Map(
  GCODE_REFERENCE.map(e => [e.code.toUpperCase(), e])
);
