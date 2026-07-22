# VHS tape for the Belay portfolio demo (docs/plan.md §10).
#
# `vhs` (https://github.com/charmbracelet/vhs) was not available in the
# sandbox this entrega (E9) was built in, so this tape has not been rendered
# here -- it's a checked-in script for whoever has `vhs` installed to run:
#
#   vhs examples/demo.tape
#
# which produces examples/demo.gif, referenced from README.md's "Demo"
# section once it exists.

Output examples/demo.gif

Set Shell "bash"
Set FontSize 16
Set Width 1200
Set Height 700
Set Padding 20

Type "python examples/demo.py --oops"
Enter
Sleep 30s
