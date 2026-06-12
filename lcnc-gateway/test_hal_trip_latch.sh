#!/usr/bin/env bash
# HAL-level regression test for the issue #34 trip-latch race.
#
# Run separately from the Python unit suite (run-tests.sh) because it needs
# LinuxCNC's `halrun` — like test_viewer_init.py, it's a realtime integration
# test. Mirrors the real chain wiring in
# examples/sim_config/hallib/lcnc_webui.hal: a servo-thread `estop_latch`
# (webui-hb-latch) whose ok-in is the heartbeat oneshot output.
#
# The bug it guards: the previous latch was a 100 ms Python poll in
# hal_watchdog.py. When a heartbeat resumed within ~1 ms of the oneshot
# expiring, the poll sampled ok-in already back TRUE, never saw the falling
# edge, and the machine silently auto-recovered from ESTOP. A servo-thread
# latch catches the FALSE in-cycle and HOLDS it until an operator reset.
#
# Exits non-zero on any failed assertion.
set -euo pipefail

command -v halrun >/dev/null || { echo "SKIP: halrun not installed"; exit 0; }

TCL="$(mktemp --suffix=.tcl)"
trap 'rm -f "$TCL"' EXIT

cat > "$TCL" <<'EOF'
loadrt threads name1=servo period1=1000000
loadrt estop_latch names=tl
addf tl servo
start
# Clear the latch (operator reset with heartbeat alive): ok-in TRUE + reset pulse.
setp tl.ok-in 1
setp tl.reset 1
after 30
setp tl.reset 0
after 30
puts "baseline=[getp tl.ok-out]"
# #34 blip: ok-in FALSE ~5 ms (≫ 1 ms servo period, ≪ old 100 ms Python poll)
# then immediately TRUE again. The servo-thread latch must catch it and HOLD.
setp tl.ok-in 0
after 5
setp tl.ok-in 1
after 30
puts "afterblip=[getp tl.ok-out]"
# A reset attempt while the heartbeat is still dead (ok-in FALSE) must NOT clear
# the latch — you can't escape ESTOP while the gateway is still unresponsive.
setp tl.ok-in 0
after 10
setp tl.reset 1
after 30
setp tl.reset 0
after 30
puts "resetwhiledead=[getp tl.ok-out]"
# Proper recovery: heartbeat alive (ok-in TRUE) + operator reset pulse.
setp tl.ok-in 1
setp tl.reset 1
after 30
setp tl.reset 0
after 30
puts "afterreset=[getp tl.ok-out]"
exit
EOF

OUT="$(halrun -f "$TCL" 2>&1)"

fail() { echo "FAIL: $1"; echo "--- halrun output ---"; echo "$OUT"; exit 1; }
check() { echo "$OUT" | grep -q "$1=$2" || fail "expected $1=$2"; }

check baseline       TRUE   # latch clear after reset → chain OK
check afterblip      FALSE  # the fix: short blip stays latched (was the race)
check resetwhiledead FALSE  # cannot reset out of a trip while heartbeat is dead
check afterreset     TRUE   # recovers once heartbeat alive + operator reset

echo "PASS: HAL trip-latch latches a sub-poll blip and holds until live reset (#34)"
