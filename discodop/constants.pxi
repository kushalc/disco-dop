# The number of unsigned longs in the bitvector of a FatChartItem.
# The maximum sentence length will be: SLOTS * sizeof(unsigned long) * 8
# These values give a maximum length of 128 bits on 32 and 64 bit systems.
IF UNAME_MACHINE == 'x86_64':
    DEF SLOTS = 2
ELSE:
    DEF SLOTS = 4

# The number of edges allocated at once
# (higher number means less overhead during allocation, but more space wasted)
# reserve 8 bytes for linked list pointer; 16 bytes per edge.
DEF EDGES_SIZE = (256 - 8) // 16

# The arity of the heap. A typical heap is binary (2).
# Higher values result in a heap with a smaller depth,
# but increase the number of comparisons between siblings that need to be done.
DEF HEAP_ARITY = 4

# context summary estimates
DEF SX = 1
DEF SXlrgaps = 2

# Any logprob above this is considered as infinity and pruned by plcfrs
DEF MAX_LOGPROB = 300.0

# The maximum length of the path to the root node and any terminal node.
# Prevents unary cycles from causing stack overflows in k-best extraction.
#
# NOTE: This is effectively a limit on how many tokens a resume can have, in
# particular assuming a complete binary tree (not true, since parse trees are
# unbalanced), 2^10 = 1K, 2^20 = 1000K, etc.
DEF MAX_DEPTH = 32

# The maximum number of NTs before we switch to a SparseCFGChart.
DEF MAX_DENSE_NTS = 20000
DEF MAX_DENSE_LEN = 512
