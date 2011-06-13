import codecs, re
from operator import mul
from array import array
from math import log, exp
from pprint import pprint
from heapq import nsmallest, heappush, heappop
from collections import defaultdict, namedtuple
from itertools import chain, count, islice, imap, repeat
from nltk import ImmutableTree, Tree, FreqDist, memoize
from dopg import nodefreq, decorate_with_ids

def srcg_productions(tree, sent, arity_marks=True, side_effect=True):
	""" given a tree with indices as terminals, and a sentence
	with the corresponding words for these indices, produce a set
	of simple RCG rules. has the side-effect of adding arity
	markers to node labels.
	>>> tree = Tree("(S (NP 1) (VP (V 0) (ADJ 2)))")
	>>> sent = "is Mary happy".split()
	>>> srcg_productions(tree, sent)
	[[('S', [[0, 1, 2]]), ('VP_2', [0, 2]), ('NP', [1])],
	[('NP', ('Mary',)), ('Epsilon', ())],
	[('VP_2', [[0], [2]]), ('V', [0]), ('ADJ', [2])],
	[('V', ('is',)), ('Epsilon', ())],
	[('ADJ', ('happy',)), ('Epsilon', ())]]
	"""
	rules = []
	for st in tree.subtrees():
		if len(st) == 1 and not isinstance(st[0], Tree):
			if sent[int(st[0])] is None: continue
			#nonterminals = (intern(str(st.node)), 'Epsilon')
			nonterminals = (st.node, 'Epsilon')
			vars = ((sent[int(st[0])],),())
			rule = zip(nonterminals, vars)
		elif isinstance(st[0], Tree):
			leaves = map(int, st.leaves())
			cnt = count(len(leaves))
			rleaves = [a.leaves() if isinstance(a, Tree) else [a] for a in st]
			rvars = [rangeheads(sorted(map(int, l))) for a,l in zip(st, rleaves)]
			lvars = list(ranges(sorted(chain(*(map(int, l) for l in rleaves)))))
			lvars = [[x for x in a if any(x in c for c in rvars)] for a in lvars]
			lhs = node_arity(st, lvars, side_effect) if arity_marks else st.node
			nonterminals = (lhs,) + tuple(node_arity(a, b) if arity_marks else a.node for a,b in zip(st, rvars))
			vars = (lvars,) + tuple(rvars)
			if vars[0][0][0] != vars[1][0]:
				# sort the right hand side so that the first argument comes
				# from the first nonterminal
				# A[0,1] -> B[1] C[0]  becomes A[0,1] -> C[0] B[1]
				# although this boils down to a simple swap in a binarized
				# grammar, for generality we do a sort instead
				vars, nonterminals = zip((vars[0], nonterminals[0]), *sorted(zip(vars[1:], nonterminals[1:]), key=lambda x: vars[0][0][0] != x[0][0]))
			rule = zip(nonterminals, vars)
		else: continue
		rules.append(rule)
	return rules

def varstoindices(rule):
	"""
	replace the variable numbers by indices pointing to the
	nonterminal on the right hand side from which they take
	their value.
	A[0,1,2] -> A[0,2] B[1]  becomes  A[0, 1, 0] -> B C

	>>> varstoindices([['S', [[0, 1, 2]]], ['NP', [1]], ['VP', [0, 2]]])
	(('S', 'NP', 'VP'), ((1, 0, 1),))
	"""
	nonterminals, vars = zip(*unfreeze(rule))
	if rule[1][0] != 'Epsilon':
		for x in vars[0]:
			for n,y in enumerate(x):
				for m,z in enumerate(vars[1:]):
					if y in z: x[n] = m
	return nonterminals, freeze(vars[0])

def induce_srcg(trees, sents, arity_marks=True):
	""" Induce a probabilistic SRCG, similar to how a PCFG is read off 
		from a treebank """
	grammar = []
	for tree, sent in zip(trees, sents):
		t = tree.copy(True)
		grammar.extend(map(varstoindices, srcg_productions(t, sent, arity_marks)))
	grammar = FreqDist(grammar)
	fd = FreqDist()
	for rule,freq in grammar.iteritems(): fd.inc(rule[0][0], freq)
	return [(rule, log(float(freq)/fd[rule[0][0]]))
				for rule, freq in grammar.iteritems()]

def dop_srcg_rules(trees, sents, normalize=False, shortestderiv=False, interpolate=1.0, wrong_interpolate=False, arity_marks=True):
	""" Induce a reduction of DOP to an SRCG, similar to how Goodman (1996)
	reduces DOP1 to a PCFG.
	Normalize means the application of the equal weights estimate
	interpolate 0.6 means 60% dop probabilities, 40% srcg rule relative frequencies"""
	ids, rules = count(1), FreqDist()
	fd, ntfd = FreqDist(), FreqDist()
	for tree, sent in zip(trees, sents):
		t = canonicalize(tree.copy(True))
		prods = map(varstoindices, srcg_productions(t, sent, arity_marks))
		ut = decorate_with_ids(t, ids)
		uprods = map(varstoindices, srcg_productions(ut, sent, False))
		nodefreq(t, ut, fd, ntfd)
		# fd: how many subtrees are headed by node X (e.g. NP or NP@12), 
		# 	counts of NP@... should sum to count of NP
		# ntfd: frequency of a node in corpus (only makes sense for exterior
		#   nodes (NP), not interior nodes (NP@12), the latter are always one)
		rules.update(chain(*([(c,avar) for c in cartpi(list(
								(x,) if x==y else (x,y)
								for x,y in zip(a,b)))]
							for (a,avar),(b,bvar) in zip(prods, uprods))))
	if interpolate != 1.0:
		srcg = dict(induce_srcg(trees, sents))
	else: srcg = defaultdict(lambda: 1)
	# should we distinguish what kind of arguments a node takes in fd?
	def prob(((rule, yf), freq)):
		return (rule, yf), (freq * reduce(mul, (fd[z] for z in rule[1:] if '@' in z), 1)
        / (float(fd[rule[0]]) * (ntfd[rule[0]] if normalize and '@' not in rule[0] else 1.0))
        * interpolate #(interpolate if '@' not in rule[0] else 1.0)
        + ((1 - interpolate) * exp(srcg[rule, yf])
            if not any('@' in z for z in rule) and not wrong_interpolate else 0))
	probmodel = [((rule, yf), log(p)) for (rule, yf), p in imap(prob, rules.iteritems()) if p]
	if shortestderiv:
		nonprobmodel = [(rule, log(1 if '@' in rule[0][0] else 0.5)) for rule in rules]
		return (nonprobmodel, dict(probmodel))
	return probmodel
	
def doubledop(trees, sents):
	from fragmentseeker import  extractfragments
	from treetransforms import minimalbinarization, complexityfanout
	backtransform = {}
	newprods = []
	# to assign IDs to ambiguous fragments (same yield)
	ids = ("#%d" % n for n in count())
	trees = list(trees)
	# adds arity markers
	srcg = FreqDist(rule for tree, sent in zip(trees, sents)
			for rule in map(varstoindices, srcg_productions(tree, sent)))
	# most work happens here
	fragments = extractfragments(trees, sents)
	productions = map(flatten, fragments)
	# construct a mapping of productions to fragments
	for prod, (frag, terminals) in zip(productions, fragments):
		if prod == frag: continue
		if prod not in backtransform:
			backtransform[prod] = frag
		else:
			if backtransform[prod]:
				newprods.append((ImmutableTree(ids.next(), prod[:]), ()))
				newprods.append((ImmutableTree(prod.node,
						[ImmutableTree(newprods[-1][0].node, [])]), terminals))
				backtransform[newprods[-1][0]] = backtransform[prod, terminals]
				backtransform[prod] = None
			newprods.append((ImmutableTree(ids.next(), prod[:]), ()))
			newprods.append((ImmutableTree(prod.node,
				[ImmutableTree(newprods[-1][0].node, [])]), terminals))
			backtransform[newprods[-1][0]] = frag
	# collect rules
	grammar = dict(rule
		for a, ((f, fsent), b) in zip(productions, fragments.iteritems())
		if backtransform.get(a, False)
		for rule in zip(map(varstoindices, srcg_productions(Tree.convert(
					minimalbinarization(a, complexityfanout, sep="}")),
				fsent, arity_marks=True, side_effect=True)),
			chain((b, ), repeat(1))))
	# ambiguous fragments
	grammar.update(rule for a, b in newprods
		for rule in zip(map(varstoindices, srcg_productions(Tree.convert(
					minimalbinarization(a, complexityfanout, sep="}")),
				b, arity_marks=a in backtransform, side_effect=True)),
			chain((fragments[backtransform[a], b] if a in backtransform else 1,),
				repeat(1))))
	# unseen srcg rules
	grammar.update((a, srcg[a]) for a in set(srcg.keys()) - set(grammar.keys()))
	# relative frequences as probabilities
	ntfd = defaultdict(float)
	for a, b in grammar.iteritems():
		ntfd[a[0][0]] += b
	for a, b in grammar.iteritems():
		grammar[a] = log(b / ntfd.get(a[0][0], b))
	return grammar.items(), backtransform

def splitgrammar(grammar):
	""" split the grammar into various lookup tables, mapping nonterminal
	labels to numeric identifiers. Also negates log-probabilities to
	accommodate min-heaps."""

	Grammar = namedtuple("Grammar", "unary lbinary rbinary lexical bylhs toid tolabel".split())
	# get a list of all nonterminals; make sure Epsilon and ROOT are first, and assign them unique IDs
	nonterminals = list(enumerate(["Epsilon", "ROOT"] + sorted(set(chain(*(rule for (rule,yf),weight in grammar))) - set(["Epsilon", "ROOT"]))))
	toid, tolabel = dict((lhs, n) for n, lhs in nonterminals), dict((n, lhs) for n, lhs in nonterminals)
	unary, lbinary, rbinary, bylhs = ([[] for _ in nonterminals] for _ in range(4))
	lexical = defaultdict(list)
	# remove sign from log probabilities because the heap we use is a min-heap
	for (rule,yf),w in grammar:
		r = tuple(toid[a] for a in rule), yf
		if len(rule) == 2:
			if r[0][1] == 0: #Epsilon
				# lexical productions (mis)use the field for the yield function to store the word
				lexical.setdefault(yf[0], []).append((r, abs(w)))
			else:
				unary[r[0][1]].append((r, abs(w)))
				bylhs[r[0][0]].append((r, abs(w)))
		elif len(rule) == 3:
			if w == 0.0: w += 0.01
			lbinary[r[0][1]].append((r, abs(w)))
			rbinary[r[0][2]].append((r, abs(w)))
			bylhs[r[0][0]].append((r, abs(w)))
		else: raise ValueError("grammar not binarized: %s" % repr(r))
	return Grammar(unary, lbinary, rbinary, lexical, bylhs, toid, tolabel)

def newsplitgrammar(grammar):
	""" split the grammar into various lookup tables, mapping nonterminal
	labels to numeric identifiers. Also negates log-probabilities to
	accommodate min-heaps."""
	from containers import Rule, Terminal
	Grammar = namedtuple("Grammar", "unary lbinary rbinary lexical bylhs toid tolabel arity".split())
	# get a list of all nonterminals; make sure Epsilon and ROOT are first, and assign them unique IDs
	nonterminals = list(enumerate(["Epsilon", "ROOT"] + sorted(set(chain(*(rule for (rule,yf),weight in grammar))) - set(["Epsilon", "ROOT"]))))
	toid, tolabel = dict((lhs, n) for n, lhs in nonterminals), dict((n, lhs) for n, lhs in nonterminals)
	unary, lbinary, rbinary, bylhs = ([[] for _ in nonterminals] for _ in range(4))
	arity = array('B', [0] * len(nonterminals))
	lexical = defaultdict(list)
	# remove sign from log probabilities because the heap we use is a min-heap
	for (rule, yf), w in grammar:
		if len(rule) == 2 and toid[rule[1]] == 0:
			r = Terminal(toid[rule[0]], toid[rule[1]], 0, unicode(yf[0]), abs(w))
			assert arity[r.lhs] in (0, 1)
			arity[r.lhs] = 1
		else:
			args, lengths = yfarray(yf)
			assert yf == arraytoyf(args, lengths)
			if len(rule) == 2 and w == 0.0: w += 0.01
			r = Rule(toid[rule[0]], toid[rule[1]],
				toid[rule[2]] if len(rule) == 3 else 0, args, lengths, abs(w))
			if arity[r.lhs] == 0:
				arity[r.lhs] = len(args)
			assert arity[r.lhs] == len(args)
		if len(rule) == 2:
			if r.rhs1 == 0: #Epsilon
				# lexical productions (mis)use the field for the yield function to store the word
				lexical.setdefault(yf[0], []).append(r)
			else:
				unary[r.rhs1].append(r)
				bylhs[r.lhs].append(r)
		elif len(rule) == 3:
			lbinary[r.rhs1].append(r)
			rbinary[r.rhs2].append(r)
			bylhs[r.lhs].append(r)
		else: raise ValueError("grammar not binarized: %s" % repr(r))
	#assert 0 not in arity[1:]
	return Grammar(unary, lbinary, rbinary, lexical, bylhs, toid, tolabel, arity)

def yfarray(yf):
	""" convert a yield function represented as a 2D sequence to an array
	object. """
	assert len(yf) <= 8 and all(len(a) <= 16 for a in yf)
	initializer = [sum(2**n*b for n, b in enumerate(a)) for a in yf]
	# 'H' for 16 bits unsigned (short), 'B' for 8 bits unsigned (char)
	return array('H', initializer), array('B', map(len, yf))

def arraytoyf(args, lengths):
	return tuple(tuple(1 if a & (1 << m) else 0 for m in range(n))
							for n, a in zip(lengths, args))

def canonicalize(tree):
	for a in reversed(list(tree.subtrees(lambda n: len(n) > 1))):
		a.sort(key=lambda n: n.leaves())
	return tree

def rangeheads(s):
	""" iterate over a sequence of numbers and yield first element of each
	contiguous range
	>>> rangeheads( (0, 1, 3, 4, 6) )
	[0, 3, 6]
	"""
	return [a[0] for a in ranges(s)]

def ranges(s):
	""" partition s into a sequence of lists corresponding to contiguous ranges
	>>> list(ranges( (0, 1, 3, 4, 6) ))
	[[0, 1], [3, 4], [6]]
	""" 
	rng = []
	for a in s:
		if not rng or a == rng[-1]+1:
			rng.append(a)
		else:
			yield rng
			rng = [a]
	if rng: yield rng

def node_arity(n, vars, inplace=False):
	""" mark node with arity if necessary """
	if len(vars) > 1 and not n.node.endswith("_%d" % len(vars)):
		if inplace: n.node = "%s_%d" % (n.node, len(vars))
		else: return "%s_%d" % (n.node, len(vars))
	return n.node if isinstance(n, Tree) else n

def alpha_normalize(s):
	""" Substitute variables so that the variables on the left-hand side appear consecutively.
		E.g. [0,1], [2,3] instead of [0,1], [3,4]. Modifies s in-place."""
	# flatten left hand side variables into a single list
	if s[0][1] == 'Epsilon': return s
	lvars = list(chain(*s[1][0]))
	for a,b in zip(lvars, range(len(lvars))):
		if a==b: continue
		for x in s[1][0]:  # left hand side
			if a in x: x[x.index(a)] = b
		for x in s[1][1:]: # right hand side
			if a in x: x[x.index(a)] = b
	return s

def freeze(l):
	return tuple(map(freeze, l)) if isinstance(l, (list, tuple)) else l

def unfreeze(l):
	return list(map(unfreeze, l)) if isinstance(l, (list, tuple)) else l

def leaves_and_frontier_nodes(tree, sent):
	"""
	>>> sent = [None, ',', None, '.']
	>>> tree = Tree("ROOT", [Tree("S_2", [0, 2]), Tree("ROOT|<$,>_2", [Tree("$,", [1]), Tree("$.", [3])])]).freeze()
	>>> print list(leaves_and_frontier_nodes(tree, sent))
	[ImmutableTree('S_2', [0, 2]), 1, 3]
	"""
	if all(not isinstance(child, Tree) for child in tree) and all(sent[child] is None for child in tree): return [tree]
	return chain(*list(leaves_and_frontier_nodes(child, sent)
			if isinstance(child, Tree) and len(child)
			else ([tree] if sent[child] is None else [child])
			for child in tree))

def flatten((tree, sent)):
	"""
	>>> sent = [None, ',', None, '.']
	>>> tree = Tree("ROOT", [Tree("S_2", [0, 2]), Tree("ROOT|<$,>_2", [Tree("$,", [1]), Tree("$.", [3])])]).freeze()
	>>> print flatten((tree, sent))
	(ROOT (S_2 0 2) (#&, 1) (#&. 3))
	"""
	return ImmutableTree(tree.node,
		[(a if isinstance(a, Tree) or sent[a] is None
		else ImmutableTree("#&" + sent[a], [a]))
		for a in leaves_and_frontier_nodes(tree, sent)]
		if all(isinstance(a, Tree) for a in tree) else tree[:])

def top_production(tree):
	return Tree(tree.node,
		[Tree(a.node, rangeheads(sorted(a.leaves())))
				if isinstance(a, Tree) else a for a in tree])

def recoverfromfragments(derivation, backtransform):
	""" Reconstruct a DOP derivation from a double DOP derivation with
	flattened fragments. """
	prod = top_production(derivation)
	def renumber(tree):
		leaves = tree.leaves()
		leafmap = dict(zip(sorted(leaves), count()))
		for a in tree.treepositions('leaves'):
			tree[a] = leafmap[tree[a]]
		return tree.freeze(), dict(zip(count(), leaves))
	rprod, leafmap = renumber(prod)
	result = Tree.convert(backtransform.get(rprod, prod))
	# revert renumbering
	for a in result.treepositions('leaves'):
		result[a] = leafmap.get(result[a], result[a])
	if (len(derivation) == 1 and isinstance(derivation[0], Tree)
		and derivation[0].node[0] == "#"):
		derivation = derivation[0]
	for r, t in zip(result.subtrees(lambda t: t.height() == 2), derivation):
		if isinstance(r, Tree):
			if isinstance(t, Tree):
				new = recoverfromfragments(t, backtransform)
				#assert r.node == new.node and len(new)
				r[:] = new[:]
			#else: print "?", r, t
			# terminals should already match.
			#assert r == t
	return result

def cartpi(seq):
	""" itertools.product doesn't support infinite sequences!
	>>> list(islice(cartpi([count(), count(0)]), 9))
	[(0, 0), (0, 1), (0, 2), (0, 3), (0, 4), (0, 5), (0, 6), (0, 7), (0, 8)]"""
	if seq: return (b + (a,) for b in cartpi(seq[:-1]) for a in seq[-1])
	return ((), )

def bfcartpi(seq):
	"""breadth-first (diagonal) cartesian product
	>>> list(islice(bfcartpi([count(), count(0)]), 9))
	[(0, 0), (0, 1), (1, 0), (1, 1), (0, 2), (1, 2), (2, 0), (2, 1), (2, 2)]"""
	#wrap items of seq in generators
	seqit = [(x for x in a) for a in seq]
	#fetch initial values
	try: seqlist = [[a.next()] for a in seqit]
	except StopIteration: return
	yield tuple(a[0] for a in seqlist)
	#bookkeeping of which iterators still have values
	stopped = len(seqit) * [False]
	n = len(seqit)
	while not all(stopped):
		if n == 0: n = len(seqit) - 1
		else: n -= 1
		if stopped[n]: continue
		try: seqlist[n].append(seqit[n].next())
		except StopIteration: stopped[n] = True; continue
		for result in cartpi(seqlist[:n] + [seqlist[n][-1:]] + seqlist[n+1:]):
			yield result

def enumchart(chart, start, tolabel, n=1):
	"""exhaustively enumerate trees in chart headed by start in top down 
		fashion. chart is a dictionary with 
		lhs -> [(insideprob, ruleprob, rhs), (insideprob, ruleprob, rhs) ... ]
		this function doesn't really belong in this file but Cython doesn't
		support generators so this function is "in exile" over here.  """
	for edge in chart[start]:
		if edge.left.label == 0: #Epsilon
			yield "(%s %d)" % (tolabel[start.label], edge.left.vec), edge.prob
		else:
			# this doesn't seem to be a good idea:
			#for x in nsmallest(n, cartpi(map(lambda y: enumchart(chart, y, tolabel, n, depth+1), rhs)), key=lambda x: sum(p for z,p in x)):
			#for x in sorted(islice(bfcartpi(map(lambda y: enumchart(chart, y, tolabel, n, depth+1), rhs)), n), key=lambda x: sum(p for z,p in x)):
			if edge.right: rhs = (edge.left, edge.right)
			else: rhs = (edge.left, )
			for x in islice(bfcartpi(map(lambda y: enumchart(chart, y, tolabel), rhs)), n):
				tree = "(%s %s)" % (tolabel[start.label], " ".join(z for z,px in x))
				yield tree, edge.prob+sum(px for z,px in x)

def exportrparse(grammar):
	""" Export an unsplitted grammar to rparse format. All frequencies are 1,
	but probabilities are exported.  """
	def repryf(yf):
		return "[" + ", ".join("[" + ", ".join("true" if a == 1 else "false" for a in b) + "]" for b in yf) + "]"
	def rewritelabel(a):
		a = a.replace("ROOT", "VROOT")
		if "|" in a:
			arity = a.rsplit("_", 1)[-1] if "_" in a[a.rindex(">"):] else "1"
			parent = a[a.index("^")+2:a.index(">",a.index("^"))] if "^" in a else ""
			parent = "^"+"-".join(x.replace("_","") if "_" in x else x+"1" for x in parent.split("-"))
			children = a[a.index("<")+1:a.index(">")].split("-")
			children = "-".join(x.replace("_","") if "_" in x else x+"1" for x in children)
			current = a.split("|")[0]
			current = "".join(current.split("_")) if "_" in current else current+"1"
			return "@^%s%s-%sX%s" % (current, parent, children, arity)
		return "".join(a.split("_")) if "_" in a else a+"1"
	for (r,yf),w in grammar:
		if r[1] != 'Epsilon':
			yield "1 %s:%s --> %s [%s]" % (repr(exp(w)), rewritelabel(r[0]), " ".join(map(rewritelabel, r[1:])), repryf(yf))

def read_bitpar_grammar(rules, lexicon, encoding='utf-8', ewe=False):
	grammar = []
	ntfd = defaultdict(float)
	ntfd1 = defaultdict(set)
	for a in codecs.open(rules, encoding=encoding):
		a = a.split()
		p, rule = float(a[0]), a[1:]
		if rule[0] == "VROOT": rule[0] = "ROOT"
		ntfd[rule[0]] += p
		ntfd1[rule[0].split("@")[0]].add(rule[0])
		grammar.append(([(rule[0], [range(len(rule) - 1)])]
					+ [(a, [n]) for n, a in enumerate(rule[1:])], p))
	for a in codecs.open(lexicon, encoding=encoding):
		a = a.split()
		word, tags = a[0], a[1:]
		tags = zip(tags[::2], map(float, tags[1::2]))
		for t,p in tags:
			ntfd[t] += p
			ntfd1[t.split("@")[0]].add(t)
		grammar.extend((((t, (word,)), ('Epsilon', ())), p) for t, p in tags)
	if ewe:
		return newsplitgrammar([(varstoindices(rule),
			log(p / (ntfd[rule[0][0]] * len(ntfd1[rule[0][0].split("@")[0]]))))
			for rule, p in grammar])
	return newsplitgrammar([(varstoindices(rule), log(p / ntfd[rule[0][0]]))
							for rule, p in grammar])

def read_penn_format(corpus, maxlen=15, n=7200):
	trees = [a for a in islice((Tree(a) for a in codecs.open(corpus, encoding='iso-8859-1')), n)
				if len(a.leaves()) <= maxlen]
	sents = [a.pos() for a in trees]
	for tree in trees:
		for n, a in enumerate(tree.treepositions('leaves')):
			tree[a] = n
	return trees, sents

def terminals(tree, sent):
	tree = Tree(tree)
	tree.node = "VROOT"
	for a, (w, t) in zip(tree.treepositions('leaves'), sent):
		tree[a] = w
	return tree.pprint(margin=999)

def write_lncky_grammar(rules, lexicon, out, encoding='utf-8'):
	""" Takes a bitpar grammar and converts it to the format of
	Mark Jonhson's cky parser. """
	grammar = []
	for a in codecs.open(rules, encoding=encoding):
		a = a.split()
		p, rule = a[0], a[1:]
		grammar.append("%s %s --> %s\n" % (p, rule[0], " ".join(rule[1:])))
	for a in codecs.open(lexicon, encoding=encoding):
		a = a.split()
		word, tags = a[0], a[1:]
		tags = zip(tags[::2], tags[1::2])
		grammar.extend("%s %s --> %s\n" % (p, t, word) for t, p in tags)
	assert "VROOT" in grammar[0]
	codecs.open(out, "w", encoding=encoding).writelines(grammar)

def rem_marks(tree):
	for a in tree.subtrees(lambda x: "_" in x.node):
		a.node = a.node.rsplit("_", 1)[0]
	for a in tree.treepositions('leaves'):
		tree[a] = int(tree[a])
	return tree

def alterbinarization(tree):
	# converts the binarization of rparse to the format that NLTK expects
	# S1 is the constituent, CS1 the parent, CARD1 the current sibling/child
	# @^S1^CS1-CARD1X1   -->  S1|<CARD1>^CS1
	#how to optionally add \2 if nonempty?
	tree = re.sub("@\^([A-Z.,()$]+)\d+(\^[A-Z.,()$]+\d+)*(?:-([A-Z.,()$]+)\d+)*X\d+", r"\1|<\3>", tree)
	# remove arity markers
	tree = re.sub(r"([A-Z.,()$]+)\d+", r"\1", tree)
	tree = re.sub("VROOT", r"ROOT", tree)
	assert "@" not in tree
	return tree

def testgrammar(grammar):
	for a,b in enumerate(grammar.bylhs):
		if b and abs(sum(exp(-rule.prob) for rule in b) - 1.0) > 0.01:
			print "Does not sum to 1:",
			print grammar.tolabel[a], sum(exp(-rule.prob) for rule in b)
			return
	tmp = defaultdict(float)
	for rr in grammar.lexical.itervalues():
		for r in rr:
			tmp[r.lhs] += exp(-r.prob)
	for a,b in tmp.iteritems():
		if abs(b - 1.0) > 0.01:
			print "Does not sum to 1:",
			print grammar.tolabel[a], b
			break
	else: print "All left hand sides sum to 1"

def bracketings(tree):
	return frozenset((a.node, frozenset(a.leaves())) for a in tree.subtrees() if a.height() > 2)

def printbrackets(brackets):
	#return ", ".join("%s[%s]" % (a, ",".join(map(str, sorted(b)))) for a,b in brackets)
	return ", ".join("%s[%s]" % (a, ",".join(map(lambda x: "%s-%s" % (x[0], x[-1])
					if len(x) > 1 else str(x[0]), ranges(sorted(b))))) for a,b in brackets)

def harmean(seq):
	try: return float(len([a for a in seq if a])) / sum(1/a if a else 0 for a in seq)
	except: return "zerodiv"

def mean(seq):
	return sum(seq) / float(len(seq)) if seq else "zerodiv"

def export(tree, sent, n):
	""" Convert a tree with indices as leafs and a sentence with the
	corresponding non-terminals to a single string in Negra's export format.
	NB: IDs do not follow the convention that IDs of children are all lower. """
	result = ["#BOS %d" % n]
	wordsandpreterminals = tree.treepositions('leaves') + [a[:-1] for a in tree.treepositions('leaves')]
	nonpreterminals = list(sorted([a for a in tree.treepositions() if a not in wordsandpreterminals and a != ()], key=len, reverse=True))
	wordids = dict((tree[a], a) for a in tree.treepositions('leaves'))
	for i, word in enumerate(sent):
		idx = wordids[i]
		result.append("\t".join((word[0],
				tree[idx[:-1]].node.replace("$[","$("),
				"--", "--",
				str(500+nonpreterminals.index(idx[:-2]) if len(idx) > 2 else 0))))
	for idx in nonpreterminals:
		result.append("\t".join(("#%d" % (500 + nonpreterminals.index(idx)),
				tree[idx].node,
				"--", "--",
				str(500+nonpreterminals.index(idx[:-1]) if len(idx) > 1 else 0))))
	result.append("#EOS %d" % n)
	return "\n".join(result) #.encode("utf-8")

def read_rparse_grammar(file):
	result = []
	for line in open(file):
		yf = eval(line[line.index("[[["):].replace("false","0").replace("true","1"))[0]
		line = line[:line.index("[[[")].split()
		line.pop(0) #freq?
		prob, lhs = line.pop(0).split(":")
		line.pop(0) # -->
		result.append(((tuple([lhs] + line), tuple(map(tuple, yf))), log(float(prob))))
	return result

def do(sent, grammar):
	try: from plcfrs_cython import parse, mostprobableparse
	except: from plcfrs import parse, mostprobableparse
	#from plcfrs import parse, mostprobableparse
	print "sentence", sent
	p, start = parse(sent, grammar, start=grammar.toid['S'])
	if p:
		mpp = mostprobableparse(p, start, grammar.tolabel)
		for t in mpp:
			print exp(mpp[t]), t
	else: print "no parse"
	print

def main():
	from treetransforms import un_collinize, collinize,\
				binarizetree, minimalbinarization, complexityfanout
	from negra import NegraCorpusReader
	from fragmentseeker import  extractfragments
	import sys, codecs
	# this fixes utf-8 output when piped through e.g. less
	# won't help if the locale is not actually utf-8, of course
	sys.stdout = codecs.getwriter('utf8')(sys.stdout)
	corpus = NegraCorpusReader(".", "sample2\.export", encoding="iso-8859-1", headorder=True, headfinal=True, headreverse=False)
	#corpus = NegraCorpusReader("../rparse", "tigerproc\.export", headorder=True, headfinal=True, headreverse=False)
	for tree, sent in zip(corpus.parsed_sents()[:3], corpus.sents()):
		print tree.pprint(margin=999)
		a = binarizetree(tree.freeze())
		print a.pprint(margin=999); print
		un_collinize(a)
		print a.pprint(margin=999), a == tree
	print
	tree = Tree("(S (VP (VP (PROAV 0) (VVPP 2)) (VAINF 3)) (VMFIN 1))")
	sent = "Daruber muss nachgedacht werden".split()
	tree = Tree("(S (NP 1) (VP (V 0) (ADV 2) (ADJ 3)))")
	sent = "is Mary very sad".split()
	tree = Tree("(S (NP 1) (VP (V 0) (ADJ 2)))")
	sent = "is Mary sad".split()
	tree2 = Tree("(S (NP 0) (VP (V 1) (ADV 2) (ADJ 3)))")
	sent2 = "Mary is really happy".split()
	#tree, sent = corpus.parsed_sents()[0], corpus.sents()[0]
	#pprint(srcg_productions(tree, sent))
	#tree.chomsky_normal_form(horzMarkov=1, vertMarkov=0)
	#tree2.chomsky_normal_form(horzMarkov=1, vertMarkov=0)
	collinize(tree, factor="right", horzMarkov=0, vertMarkov=0, tailMarker='', minMarkov=1)
	collinize(tree2, factor="right", horzMarkov=0, vertMarkov=0, tailMarker='', minMarkov=1)
	for (r, yf), w in sorted(induce_srcg([tree.copy(True), tree2], [sent, sent2]),
								key=lambda x: (x[0][0][1] == 'Epsilon', x)):
		print "%.2f %s --> %s\t%r" % (exp(w), r[0], " ".join(r[1:]), list(yf))
	print
	for ((r, yf), w1), (r2, w2) in zip(dop_srcg_rules([tree.copy(True), tree2], [sent, sent2], interpolate=1.0),
									dop_srcg_rules([tree.copy(True), tree2], [sent, sent2], interpolate=0.5)):
		assert (r, yf) == r2
		print "%.2f %.2f %s --> %s\t%r" % (exp(w1), exp(w2), r[0], " ".join(r[1:]), list(yf))

	for a in sorted(exportrparse(induce_srcg([tree.copy(True)], [sent]))): print a

	pprint(splitgrammar(induce_srcg([tree.copy(True)], [sent])))
	#pprint(dop_srcg_rules([tree.copy(True)], [sent], interpolate=0.5))
	do(sent, newsplitgrammar(dop_srcg_rules([tree], [sent])))
	newsplitgrammar(dop_srcg_rules([tree], [sent]))
	trees = list(corpus.parsed_sents()[:10])
	sents = corpus.sents()[:100]
	[a.chomsky_normal_form(horzMarkov=1) for a in trees]
	grammar, backtransform = doubledop(trees, sents)
	print 'grammar',
	for (r, yf), w in sorted(grammar):
		print "%.2f %s --> %s\t%r" % (exp(w), r[0], " ".join(r[1:]), list(yf))
	print "backtransform: {",
	for a,b in backtransform.items():
		try: print a,
		except: print a.pprint(),
		print ":", b
	print "}"
	grammar = newsplitgrammar(grammar)
	testgrammar(grammar)
	try: from plcfrs_cython import parse, mostprobableparse
	except: from plcfrs import parse, mostprobableparse
	for tree, sent in zip(corpus.parsed_sents(), sents[:10]):
		print "sentence", sent
		p, start = parse(sent, grammar, start=grammar.toid['ROOT'], exhaustive=True)
		print "gold", canonicalize(tree)
		print "parsetrees:"
		if p:
			mpp = mostprobableparse(p, start, grammar.tolabel)
			for t in mpp:
				t2 = Tree(t)
				for idx in t2.treepositions('leaves'): t2[idx] = int(t2[idx])
				un_collinize(t2, childChar="}")
				r = recoverfromfragments(canonicalize(t2), backtransform)
				un_collinize(r)
				r = rem_marks(r)
				print mpp[t], rem_marks(tree) == r, r
		else: print "no parse"
		print

if __name__ == '__main__':
	from doctest import testmod, NORMALIZE_WHITESPACE, ELLIPSIS
	# do doctests, but don't be pedantic about whitespace (I suspect it is the
	# militant anti-tab faction who are behind this obnoxious default)
	fail, attempted = testmod(verbose=False, optionflags=NORMALIZE_WHITESPACE | ELLIPSIS)
	if attempted and not fail: print "%d doctests succeeded!" % attempted
	main()
