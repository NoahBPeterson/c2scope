"""Inverses of documented c2 / c1xx decisions: from the original's bytes (plus lifted IL) to the source property that
produces them.  docs/c2/invert.md states each rule, its evidence and its measured accuracy.

  odframe   /Od frame layout -> local names and declaration order (c1xx's block-scope hash)
  operands  commutative operand order -> which operand the source writes first (symkeys' weight)
  regorder  callee-saved register roles -> definition / use order constraints (rasim's priorities)
  regfix    regorder applied to a lifted unit (one traced compile, the moves, one verifying compile)
  x87names  x87 rounding points -> which values are float locals / members / kept in expressions
  inlining  inlined call sites -> which callees must be inline / visible / defined first; inlined-body detection
  schedorder  list-scheduler ties -> statement order constraints (on schedwant's original order)
"""
