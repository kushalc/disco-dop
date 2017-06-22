"""CKY parser for Probabilistic Context-Free Grammar (PCFG)."""
from __future__ import print_function
from os import unlink
import logging
import math
import re
import sys
import subprocess
from math import exp, log as pylog
from array import array
from itertools import count
import numpy as np
from .tree import Tree
from .util import which
from .plcfrs import DoubleAgenda
from .treebank import TERMINALSRE

cimport cython
include "constants.pxi"

cdef double INFINITY = float('infinity')


cdef class CFGChart(Chart):
    """A Chart for context-free grammars (CFG).

    An item is a Python integer made up of ``start``, ``end``, ``lhs`` indices.
    """

    def __init__(self, Grammar grammar, list sent,
                 start=None, logprob=True, viterbi=True):
        raise NotImplementedError

    cdef _left(self, item, Edge * edge):
        cdef short start
        if edge.rule is NULL:
            return None
        start = <size_t > item / (
            self.grammar.nonterminals * self.lensent)
        return cellidx(start, edge.pos.mid, self.lensent,
                       self.grammar.nonterminals) + edge.rule.rhs1

    cdef _right(self, item, Edge * edge):
        cdef short end
        if edge.rule is NULL or edge.rule.rhs2 == 0:
            return None
        end = <size_t > item / self.grammar.nonterminals % self.lensent + 1
        return cellidx(edge.pos.mid, end, self.lensent,
                       self.grammar.nonterminals) + edge.rule.rhs2

    def root(self):
        return cellidx(0, self.lensent,
                       self.lensent, self.grammar.nonterminals) + self.start

    cdef uint32_t label(self, item):
        return < size_t > item % self.grammar.nonterminals

    def indices(self, item):
        cdef short start = (item // self.grammar.nonterminals) // self.lensent
        cdef short end = (item // self.grammar.nonterminals) % self.lensent + 1
        return list(range(start, end))

    def itemstr(self, item):
        cdef uint32_t lhs = self.label(item)
        cdef short start = (item // self.grammar.nonterminals) // self.lensent
        cdef short end = (item // self.grammar.nonterminals) % self.lensent + 1
        return '%s[%d:%d]' % (
            self.grammar.tolabel[lhs], start, end)

    def getitems(self):
        return self.parseforest


@cython.final
cdef class DenseCFGChart(CFGChart):
    """
    A CFG chart in which edges and probabilities are stored in a dense
    array; i.e., array is contiguous and all valid combinations of indices
    ``0 <= start <= mid <= end`` and ``label`` can be addressed. Whether it is
    feasible to use this chart depends on the grammar constant, specifically
    the number of non-terminal labels."""

    def __init__(self, Grammar grammar, list sent,
                 start=None, logprob=True, viterbi=True):
        self.grammar = grammar
        self.sent = sent
        self.lensent = len(sent)
        self.start = grammar.toid[grammar.start if start is None else start]
        self.logprob = logprob
        self.viterbi = viterbi
        entries = compactcellidx(self.lensent - 1, self.lensent, self.lensent,
                                 grammar.nonterminals) + grammar.nonterminals
        self.probs = <double * >malloc(entries * sizeof(double))
        if self.probs is NULL:
            raise MemoryError('allocation error')
        for n in range(entries):
            self.probs[n] = INFINITY
        # store parse forest in array instead of dict
        # FIXME: use compactcellidx?
        entries = cellidx(self.lensent - 1, self.lensent, self.lensent,
                          grammar.nonterminals) + grammar.nonterminals
        self.parseforest = <EdgesStruct * >calloc(entries, sizeof(EdgesStruct))
        if self.parseforest is NULL:
            raise MemoryError('allocation error')
        self.itemsinorder = array(b'L' if PY2 else 'L')

    def __dealloc__(self):
        cdef size_t n, entries = cellidx(
            self.lensent - 1, self.lensent, self.lensent,
            self.grammar.nonterminals) + self.grammar.nonterminals
        cdef MoreEdges * cur
        cdef MoreEdges * tmp
        if self.probs is not NULL:
            free(self.probs)
        for n in range(entries):
            cur = self.parseforest[n].head
            while cur is not NULL:
                tmp = cur
                cur = cur.prev
                free(tmp)
        free(self.parseforest)

    cdef void addedge(self, uint32_t lhs, Idx start, Idx end, Idx mid,
                      ProbRule * rule):
        """Add new edge to parse forest."""
        cdef size_t item = cellidx(
            start, end, self.lensent, self.grammar.nonterminals) + lhs
        cdef Edge * edge
        cdef EdgesStruct * edges = &(self.parseforest[item])
        cdef MoreEdges * edgelist
        if edges.head is NULL:
            edgelist = <MoreEdges * >calloc(1, sizeof(MoreEdges))
            if edgelist is NULL:
                abort()
            edgelist.prev = NULL
            edges.head = edgelist
            self.itemsinorder.append(item)
        else:
            edgelist = edges.head
            if edges.len == EDGES_SIZE:
                edgelist = <MoreEdges * >calloc(1, sizeof(MoreEdges))
                if edgelist is NULL:
                    abort()
                edgelist.prev = edges.head
                edges.head = edgelist
                edges.len = 0
        edge = &(edgelist.data[edges.len])
        edge.rule = rule
        edge.left = start
        edge.pos.mid = mid
        edges.len += 1

    cdef bint updateprob(self, uint32_t lhs, Idx start, Idx end, double prob,
                         double beam):
        """Update probability for item if better than current one."""
        cdef size_t idx = compactcellidx(
            start, end, self.lensent, self.grammar.nonterminals) + lhs
        cdef size_t beamitem
        if beam:
            # the item with label 0 for a cell holds the best score in a cell
            beamitem = compactcellidx(
                start, end, self.lensent, self.grammar.nonterminals)
            if prob > self.probs[beamitem] + beam:
                return False
            elif prob < self.probs[beamitem]:
                self.probs[beamitem] = self.probs[idx] = prob
            elif prob < self.probs[idx]:
                self.probs[idx] = prob
        elif prob < self.probs[idx]:
            self.probs[idx] = prob
        return True

    cdef double _subtreeprob(self, size_t item):
        """Get viterbi / inside probability of a subtree headed by `item`."""
        cdef short start, end
        cdef uint32_t lhs
        cdef size_t idx
        lhs = item % self.grammar.nonterminals
        item /= self.grammar.nonterminals
        start = item / self.lensent
        end = item % self.lensent + 1
        idx = compactcellidx(
            start, end, self.lensent, self.grammar.nonterminals) + lhs
        return self.probs[idx]

    cdef double subtreeprob(self, item):
        return self._subtreeprob( < size_t > item)

    def getitems(self):
        return self.itemsinorder
        # return [n for n, a in enumerate(self.parseforest) if a is not None]

    cdef Edges getedges(self, item):
        """Get edges for item."""
        if item is None:
            return None
        result = Edges()
        result.len = self.parseforest[item].len
        result.head = self.parseforest[item].head
        return result

    cpdef bint hasitem(self, item):
        """Test if item is in chart."""
        return (item is not None
                        and self.parseforest[ < size_t > item].head is not NULL)

    def setprob(self, item, double prob):
        """Set probability for item (unconditionally)."""
        cdef short start, end
        cdef uint32_t lhs
        cdef size_t idx
        lhs = item % self.grammar.nonterminals
        item /= self.grammar.nonterminals
        start = item / self.lensent
        end = item % self.lensent + 1
        idx = compactcellidx(
            start, end, self.lensent, self.grammar.nonterminals) + lhs
        self.probs[idx] = prob

    def __bool__(self):
        """Return true when the root item is in the chart.

        i.e., test whether sentence has been parsed successfully."""
        return self.parseforest[self.root()].head is not NULL


@cython.final
cdef class SparseCFGChart(CFGChart):
    """
    A CFG chart which uses a dictionary for each cell so that grammars
    with a large number of non-terminal labels can be handled."""

    def __init__(self, Grammar grammar, list sent,
                 start=None, logprob=True, viterbi=True):
        self.grammar = grammar
        self.sent = sent
        self.lensent = len(sent)
        self.start = grammar.toid[grammar.start if start is None else start]
        self.logprob = logprob
        self.viterbi = viterbi
        self.probs = {}
        self.parseforest = {}
        self.itemsinorder = array(b'L' if PY2 else 'L')

    def __dealloc__(self):
        cdef MoreEdges * cur
        cdef MoreEdges * tmp
        for item in self.parseforest:
            cur = ( < Edges > self.parseforest[item]).head
            while cur is not NULL:
                tmp = cur
                cur = cur.prev
                free(tmp)

    cdef void addedge(self, uint32_t lhs, Idx start, Idx end, Idx mid,
                      ProbRule * rule):
        """Add new edge to parse forest."""
        cdef Edges edges
        cdef MoreEdges * edgelist
        cdef Edge * edge
        cdef size_t item = cellidx(
            start, end, self.lensent, self.grammar.nonterminals) + lhs
        if item in self.parseforest:
            edges = self.parseforest[item]
            edgelist = edges.head
            if edges.len == EDGES_SIZE:
                edgelist = <MoreEdges * >calloc(1, sizeof(MoreEdges))
                if edgelist is NULL:
                    abort()
                edgelist.prev = edges.head
                edges.head = edgelist
                edges.len = 0
        else:
            edges = Edges()
            self.parseforest[item] = edges
            edgelist = <MoreEdges * >calloc(1, sizeof(MoreEdges))
            if edgelist is NULL:
                abort()
            edges.head = edgelist
            self.itemsinorder.append(item)
        edge = &(edgelist.data[edges.len])
        edge.rule = rule
        edge.left = start
        edge.pos.mid = mid
        edges.len += 1

    cdef bint updateprob(self, uint32_t lhs, Idx start, Idx end, double prob,
                         double beam):
        """Update probability for item if better than current one."""
        cdef size_t item = cellidx(
            start, end, self.lensent, self.grammar.nonterminals) + lhs
        cdef size_t beamitem
        if beam:
            # the item with label 0 for a cell holds the best score in a cell
            beamitem = cellidx(
                start, end, self.lensent, self.grammar.nonterminals)
            if beamitem not in self.probs or prob < self.probs[beamitem]:
                self.probs[item] = self.probs[beamitem] = prob
            elif prob > self.probs[beamitem] + beam:
                return False
            elif item not in self.probs or prob < self.probs[item]:
                self.probs[item] = prob
        elif item not in self.probs or prob < self.probs[item]:
            self.probs[item] = prob
        return True

    cdef double _subtreeprob(self, size_t item):
        """Get viterbi / inside probability of a subtree headed by `item`."""
        return (PyFloat_AS_DOUBLE(self.probs[item])
                if item in self.probs else INFINITY)

    cdef double subtreeprob(self, item):
        return self._subtreeprob(item)

    cpdef bint hasitem(self, item):
        """Test if item is in chart."""
        return item in self.parseforest

    def setprob(self, item, prob):
        """Set probability for item (unconditionally)."""
        self.probs[item] = prob


def parse(sent, Grammar grammar, tags=None, start=None, beamer=None):
    """A CKY parser modeled after Bodenstab's 'fast grammar loop'.

    :param sent: A sequence of tokens that will be parsed.
    :param grammar: A ``Grammar`` object.
    :param tags: Optionally, a sequence of POS tags to use instead of
            attempting to apply all possible POS tags.
    :param start: integer corresponding to the start symbol that complete
            derivations should be headed by; e.g., ``grammar.toid['ROOT']``.
            If not given, the default specified by ``grammar`` is used.

    :returns: a ``Chart`` object.
    """
    if grammar.maxfanout != 1:
        raise ValueError('Not a PCFG! fanout: %d' % grammar.maxfanout)
    if not grammar.logprob:
        raise ValueError('Expected grammar with log probabilities.')

    if grammar.nonterminals < MAX_DENSE_NTS and \
       len(sent) < MAX_DENSE_LEN:
        chart = DenseCFGChart(grammar, sent, start)
        return _parse_clean(sent, < DenseCFGChart > chart, grammar, tags, beamer)
    else:
        chart = SparseCFGChart(grammar, sent, start)
        return _parse_clean(sent, < SparseCFGChart > chart, grammar, tags, beamer)

cdef _parse_clean(sent, CFGChart_fused chart, Grammar grammar, tags=None, beamer=None):
    cdef:
        DoubleAgenda unaryagenda = DoubleAgenda()
        short[:, :] minleft, maxleft, minright, maxright
        ProbRule * rule
        short left, right, mid, span, lensent = len(sent)
        short narrowl, narrowr, minmid, maxmid
        double oldscore, prob
        uint32_t n, lhs = 0
        size_t cell

    prepared_doc = grammar.emission._prepare_doc(sent) if grammar.emission else None
    minleft, maxleft, minright, maxright = minmaxmatrices(grammar.nonterminals, lensent)
    covered, msg = _populate_pos(grammar, chart, unaryagenda, sent, tags,
                                 minleft, maxleft, minright, maxright,
                                 prepared_doc)
    if not covered:
        return chart, msg

    for span in range(1, lensent + 1):
        spannable = grammar.emission._spannable(span, prepared=prepared_doc) if grammar.emission \
                    else xrange(grammar.phrasalnonterminals)
        beam = beamer(span) if beamer else 0.000

        for left in range(lensent - span + 1):
            right = left + span

            # NOTE: lastidx isn't used for awhile, but it's necessary for
            # _handle_unary to work since it'll do one pass on all LHSes added
            # for this (left, span) pair.
            lastidx = len(chart.itemsinorder)
            cell = cellidx(left, right, lensent, grammar.nonterminals)
            prepared_span = grammar.emission._prepare_span(sent[left:right], prepared=prepared_doc) \
                            if grammar.emission else None

            for lhs in spannable:
                oldscore = chart._subtreeprob(cell + lhs)
                if grammar._is_mte(lhs):
                    prob = grammar.emission._span_log_proba(lhs, sent[left:right],
                                                            prepared=prepared_span)
                    if not math.isinf(prob) and not math.isnan(prob):
                        if chart.updateprob(lhs, left, right, prob, beam):
                            chart.addedge(lhs, left, right, right, NULL)

                elif span > 1:
                    for n in xrange(grammar.numbinary):
                        rule = &(grammar.bylhs[lhs][n])
                        if rule.lhs != lhs:
                            break
                        elif not rule.rhs2:
                            continue

                        narrowr = minright[rule.rhs1, left]
                        narrowl = minleft[rule.rhs2, right]
                        if narrowr >= right or narrowl < narrowr:
                            continue

                        minmid = max(narrowr, maxleft[rule.rhs2, right])
                        maxmid = min(narrowl, maxright[rule.rhs1, left])
                        for mid in range(minmid, maxmid + 1):
                            leftitem = cellidx(left, mid, lensent, grammar.nonterminals) + rule.rhs1
                            rightitem = cellidx(mid, right, lensent, grammar.nonterminals) + rule.rhs2
                            if (chart.hasitem(leftitem) and chart.hasitem(rightitem)):
                                prob = (rule.prob + chart._subtreeprob(leftitem) +
                                        chart._subtreeprob(rightitem))
                                if chart.updateprob(lhs, left, right, prob, beam):
                                    chart.addedge(lhs, left, right, mid, rule)

                # update filter
                if isinf(oldscore):
                    if not chart.hasitem(cell + lhs):
                        continue
                    _update_filter(chart, lhs, left, right, minleft, maxleft,
                                   minright, maxright)

            _handle_unary(chart, grammar, unaryagenda, cell, lastidx,
                          left, right, lensent, minleft, maxleft, minright,
                          maxright)

    if not chart:
        return chart, 'no parse ' + chart.stats()
    return chart, chart.stats()

cdef _update_filter(CFGChart_fused chart, uint32_t lhs,
                    short left, short right,
                    short[:, :] minleft, short[:, :] maxleft,
                    short[:, :] minright, short[:, :] maxright):
    if left > minleft[lhs, right]:
        minleft[lhs, right] = left
    if left < maxleft[lhs, right]:
        maxleft[lhs, right] = left
    if right < minright[lhs, left]:
        minright[lhs, left] = right
    if right > maxright[lhs, left]:
        maxright[lhs, left] = right

cdef _handle_unary(CFGChart_fused chart, Grammar grammar, DoubleAgenda unaryagenda,
                   size_t cell, size_t lastidx, short left, short right, short lensent,
                   short[:, :] minleft, short[:, :] maxleft,
                   short[:, :] minright, short[:, :] maxright):
    cdef:
        ProbRule * rule
        double prob
        uint32_t n, lhs, rhs1

    # unary rules
    unaryagenda.update_entries([
        new_DoubleEntry(chart.label(item), chart._subtreeprob(item), 0)
        for item in chart.itemsinorder[lastidx:]
    ])
    while unaryagenda.length:
        rhs1 = unaryagenda.popentry().key
        for n in range(grammar.numunary):
            rule = &(grammar.unary[rhs1][n])
            if rule.rhs1 != rhs1:
                break

            lhs = rule.lhs
            prob = rule.prob + chart._subtreeprob(cell + rhs1)
            chart.addedge(lhs, left, right, right, rule)
            if (not chart.hasitem(cell + lhs) or
                prob < chart._subtreeprob(cell + lhs)):
                chart.updateprob(lhs, left, right, prob, 0.0)
                unaryagenda.setifbetter(lhs, prob)

            _update_filter(chart, lhs, left, right, minleft, maxleft,
                           minright, maxright)
    unaryagenda.clear()

cdef _populate_pos(Grammar grammar, CFGChart_fused chart, DoubleAgenda unaryagenda,
                   sent, tags,
                   short[:, :] minleft, short[:, :] maxleft,
                   short[:, :] minright, short[:, :] maxright,
                   prepared=None):
    """Apply all possible lexical and unary rules on each lexical span.

    :returns: a tuple ``(success, msg)`` where ``success`` is True if a POS tag
    was found for every word in the sentence."""
    cdef:
        LexicalRule lexrule
        uint32_t lhs
        short left, right, lensent = len(sent)
        size_t cell, lastidx = len(chart.itemsinorder)

    for left, word in enumerate(sent):
        tag = tags[left] if tags else None
        # if we are given gold tags, make sure we only allow matching
        # tags - after removing addresses introduced by the DOP reduction
        # and other state splits.
        tagre = re.compile('%s($|@|\\^|/)' % re.escape(tag)) if tag else None
        right = left + 1
        recognized = False

        # FIXME: Have parser return -Inf or NaN if not emittable. DiscoPCFG
        # uses grammar specificity as an optimization and we're breaking that by
        # using all possible lexrules here.
        orth = unicode(word)
        cell = cellidx(left, right, lensent, grammar.nonterminals)
        for lexrule in grammar.lexical if grammar.emission else \
                       grammar.lexicalbyword.get(orth, ()):
            lhs = lexrule.lhs
            if tag is None or tagre.match(grammar.tolabel[lhs]):
                pr = grammar.emission._token_log_proba(lhs, word, prepared=prepared) \
                     if grammar.emission else lexrule.prob
                if math.isinf(pr) or math.isnan(pr):
                    continue
                recognized |= True
                unaryagenda.setitem(lhs, 0.0)
                chart.addedge(lhs, left, right, right, NULL)
                chart.updateprob(lhs, left, right, pr, 0.0)

                # logging.debug("Added to UnaryAgenda: %s [%s] => %0.3f",
                #               orth.strip(), grammar.tolabel[lhs], pr)
                _update_filter(chart, lhs, left, right, minleft, maxleft,
                               minright, maxright)

        if not recognized:
            if tag is None and orth not in grammar.lexicalbyword:
                return chart, 'no parse: %r not in lexicon' % word
            elif tag is not None and tag not in grammar.toid:
                return chart, 'no parse: unknown tag %r' % tag
            return chart, 'no parse: all tags for %r blocked' % word

        _handle_unary(chart, grammar, unaryagenda, cell, lastidx, left, right, lensent,
                      minleft, maxleft, minright, maxright)

    return True, ''


def minmaxmatrices(nonterminals, lensent):
    """Create matrices to track minima and maxima for binary splits."""
    minleft = np.empty((nonterminals, lensent + 1), dtype='int16')
    maxleft = np.empty_like(minleft)
    minleft[...], maxleft[...] = -1, lensent + 1
    minright, maxright = maxleft.copy(), minleft.copy()
    return minleft, maxleft, minright, maxright


BITPARUNESCAPE = re.compile(r"\\([\"\\ $\^'()\[\]{}=<>#])")
BITPARPARSES = re.compile(r'^vitprob=(.*)\n(\(.*\))\n', re.MULTILINE)
BITPARPARSESLOG = re.compile(r'^logvitprob=(.*)\n(\(.*\))\n', re.MULTILINE)
CPUTIME = re.compile('^raw cpu time (.+)$', re.MULTILINE)
LOG10 = pylog(10)


def parse_bitpar(grammar, rulesfile, lexiconfile, sent, n,
                 startlabel, startid, tags=None):
    """Parse a sentence with bitpar, given filenames of rules and lexicon.

    :param n: the number of derivations to return (max 1000); if n == 0, return
            parse forest instead of n-best list (requires binarized grammar).
    :returns: a dictionary of derivations with their probabilities."""
    if n < 1 or n > 1000:
        raise ValueError('with bitpar number of derivations n should be '
                         '1 <= n <= 1000. got: n = %d' % n)
    chart = SparseCFGChart(grammar, sent, start=startlabel,
                           logprob=True, viterbi=True)
    if n == 0:
        if not chart.grammar.binarized:
            raise ValueError('Extracing parse forest, '
                             'expected binarized grammar.')
    else:
        chart.rankededges = {chart.root(): []}
    tmp = None
    if tags:
        import tempfile
        tmp = tempfile.NamedTemporaryFile(delete=False)
        # NB: this doesn't work with the tags from the DOP reduction
        tmp.writelines(set(['%s@%s\t%s@%s 1\t%s 1\n' % (t, w, t, w, t)
                            for t, w in zip(tags, sent)]))
        tmp.close()
        lexiconfile = tmp.name
    tokens = [token.encode('utf8') for token in sent]
    if tags:
        tokens = ['%s@%s' % (tag, token) for tag, token in zip(tags, tokens)]
    # pass empty 'unkwown word file' to disable bitpar's smoothing
    args = ['-y'] if n == 0 else ['-b', str(n)]
    args += ['-s', startlabel, '-vp', '-u', '/dev/null', rulesfile, lexiconfile]
    proc = subprocess.Popen([which('bitpar')] + args,
                            shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    results, msg = proc.communicate(('\n'.join(tokens) + '\n').encode('utf8'))
    msg = msg.replace('Warning: Word class 0 did not occur!\n',
                      '').decode('utf8').strip()
    match = CPUTIME.search(msg)
    cputime = float(match.group(1)) if match else 0.0
    if tags:
        unlink(tmp.name)
    # decode results or not?
    if not results or results.startswith('No parse'):
        return chart, cputime, '%s\n%s' % (results.decode('utf8').strip(), msg)
    elif n == 0:
        bitpar_yap_forest(results, chart)
    else:
        bitpar_nbest(results, chart)
    return chart, cputime, ''


def bitpar_yap_forest(forest, SparseCFGChart chart):
    """Read bitpar YAP parse forest (-y option) into a Chart object.

    The forest has lines of the form::
            label start end     prob [edge1] % prob [edge2] % .. %%

    where an edge is either a quoted "word", or a rule number and one or two
    line numbers in the parse forest referring to children.
    Assumes binarized grammar. Assumes chart's Grammar object has same order of
    grammar rules as the grammar that was presented to bitpar."""
    cdef ProbRule * rule
    cdef uint32_t lhs
    cdef Idx left, right, mid
    cdef size_t ruleno, child1
    cdef double prob
    if not chart.grammar.binarized:
        raise ValueError('Extracing parse forest, expected binarized grammar.')
    forest = forest.strip().splitlines()
    midpoints = [int(line.split(None, 3)[2]) for line in forest]
    for line in forest:
        a, b, c, fields = line.rstrip('% ').split(None, 3)
        lhs, left, right = chart.grammar.toid[a], int(b), int(c)
        # store 1-best probability, other probabilities can be ignored.
        prob = -pylog(float(fields.split(None, 1)[0]))
        chart.updateprob(lhs, left, right, prob, 0.0)
        for edge in fields.split(' % '):
            unused_prob, rest = edge.split(None, 1)
            if rest.startswith('"'):
                mid = right
                rule = NULL
            else:
                restsplit = rest.split(None, 2)
                ruleno = int(restsplit[0])
                child1 = int(restsplit[1])
                # ignore second child: (midpoint + end of current node suffices)
                # child2 = restsplit[2] if len(restsplit) > 2 else None
                mid = midpoints[child1]
                rule = &(chart.grammar.bylhs[0][chart.grammar.revmap[ruleno]])
            chart.addedge(lhs, left, right, mid, rule)


def bitpar_nbest(nbest, SparseCFGChart chart):
    """Put bitpar's list of n-best derivations into the chart.
    Parse forest is not converted."""
    lines = BITPARUNESCAPE.sub(r'\1', nbest).replace(')(', ') (')
    derivs = [(renumber(deriv), -float(prob) * LOG10)
              for prob, deriv in BITPARPARSESLOG.findall(lines)]
    if not derivs:
        derivs = [(renumber(deriv), -pylog(float(prob) or 5.e-130))
                  for prob, deriv in BITPARPARSES.findall(lines)]
    chart.parseforest = {chart.root(): None}  # dummy so bool(chart) == True
    chart.rankededges[chart.root()] = derivs


def renumber(deriv):
    """Replace terminals of CF-derivation (string) with indices."""
    it = count()
    return TERMINALSRE.sub(lambda _: ' %s)' % next(it), deriv)


def test():
    from .containers import Grammar
    from .disambiguation import getderivations, marginalize
    from operator import itemgetter
    cfg = Grammar([
        ((('A', 'A'), ((0, ), )), 0.7), ((('A', 'B'), ((0, ), )), 0.6),
        ((('A', 'C'), ((0, ), )), 0.5), ((('A', 'D'), ((0, ), )), 0.4),
        ((('B', 'A'), ((0, ), )), 0.3), ((('B', 'B'), ((0, ), )), 0.2),
        ((('B', 'C'), ((0, ), )), 0.1), ((('B', 'D'), ((0, ), )), 0.2),
        ((('B', 'C'), ((0, ), )), 0.3), ((('C', 'A'), ((0, ), )), 0.4),
        ((('C', 'B'), ((0, ), )), 0.5), ((('C', 'C'), ((0, ), )), 0.6),
        ((('C', 'D'), ((0, ), )), 0.7), ((('D', 'A'), ((0, ), )), 0.8),
        ((('D', 'B'), ((0, ), )), 0.9), ((('D', 'NP', 'VP'), ((0, 1), )), 1),
        ((('D', 'C'), ((0, ), )), 0.8), ((('S', 'D'), ((0, ), )), 0.5),
        ((('S', 'A'), ((0, ), )), 0.8), ((('NP', 'Epsilon'), ('mary', )), 1),
        ((('VP', 'Epsilon'), ('walks', )), 1)],
        start='S')
    print(cfg)
    print('cfg parsing; sentence: mary walks')
    print('pcfg')
    chart, msg = parse('mary walks'.split(), cfg)
    assert chart, msg
    # chart, msg = parse_sparse('mary walks'.split(), cfg)
    # assert chart, msg
    print(chart)
    rules = [
        ((('NP', 'NP', 'PP'), ((0, 1), )), 0.4),
        ((('PP', 'P', 'NP'), ((0, 1), )), 1),
        ((('S', 'NP', 'VP'), ((0, 1), )), 1),
        ((('VP', 'V', 'NP'), ((0, 1), )), 0.7),
        ((('VP', 'VP', 'PP'), ((0, 1), )), 0.3),
        ((('NP', 'Epsilon'), ('astronomers', )), 0.1),
        ((('NP', 'Epsilon'), ('ears', )), 0.18),
        ((('V', 'Epsilon'), ('saw', )), 1),
        ((('NP', 'Epsilon'), ('saw', )), 0.04),
        ((('NP', 'Epsilon'), ('stars', )), 0.18),
        ((('NP', 'Epsilon'), ('telescopes', )), 0.1),
        ((('P', 'Epsilon'), ('with', )), 1)]
    cfg2 = Grammar(rules, start='S')
    sent = 'astronomers saw stars with telescopes'.split()
    cfg2.switch(u'default', True)
    chart, msg = parse(sent, cfg2)
    print(msg)
    print(chart)
    derivations, entries = getderivations(chart, 10, True, False, True)
    mpp, _ = marginalize('mpp', derivations, entries, chart)
    for a, p, _ in sorted(mpp, key=itemgetter(1), reverse=True):
        print(p, a)
    # chart1, msg1 = parse(sent, cfg2, symbolic=True)
    # print(msg, '\n', msg1)


__all__ = ['CFGChart', 'DenseCFGChart', 'SparseCFGChart', 'parse', 'renumber',
           'minmaxmatrices', 'parse_bitpar', 'bitpar_yap_forest', 'bitpar_nbest']
