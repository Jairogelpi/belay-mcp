# VHS tape for the Belay portfolio demo (docs/plan.md §10, E23 Task 4).
#
# Run from the REPOSITORY ROOT (VHS's recorded shell starts in whatever
# directory `vhs` itself is invoked from -- this tape does not `cd`, so
# the one committed command below only resolves correctly from there):
#
#   vhs examples/demo.tape
#
# Regenerates docs/assets/belay-demo.gif from examples/demo.py's real,
# live output (see README.md's "Demo" section) -- never hand-edited.

Output docs/assets/belay-demo.gif

Set Shell "bash"
Set FontSize 16
Set Width 1200
Set Height 700
Set Padding 20
# Deterministic recording: fixed typing speed (no randomized per-key
# jitter) and a generous, fixed settle time after the command finishes --
# never a screen-content-dependent wait, so the same tape produces the
# same-length GIF on any machine.
Set TypingSpeed 50ms

Type "python examples/demo.py --oops"
Enter
Sleep 45s
