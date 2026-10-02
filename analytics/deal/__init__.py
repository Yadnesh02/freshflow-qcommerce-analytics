"""The deal rail's own analysis (tasks D2-D5).

Separate from `analytics/optimization/deal_slots.py` on purpose. That module
*decides* which SKUs get a slot; this package *measures* what the slots did.
Keeping the decision and its evaluation apart is what stops the allocator being
graded by its own assumptions - it currently optimises against coefficients that
nothing in the repo measured, and D5 only gets to re-point it once the numbers
here exist.
"""
