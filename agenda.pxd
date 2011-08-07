from containers cimport Edge
from cpython cimport PyList_Append as append,\
					PyList_GET_ITEM as list_getitem,\
					PyList_GET_SIZE as list_getsize,\
					PyDict_Contains as dict_contains,\
					PyDict_GetItem as dict_getitem

cdef class Entry:
	cdef public object key
	cdef public Edge value
	cdef unsigned long count

cdef class heapdict(dict):
	cdef dict mapping
	cdef public list heap
	cdef public unsigned long length
	cdef unsigned long counter
	cpdef Entry popentry(heapdict self)
	cdef inline Edge getitem(heapdict self, key)
	cdef inline void setitem(heapdict self, key, Edge value)
	cdef inline void setifbetter(heapdict self, key, Edge value)
	cdef inline bint contains(heapdict self, key)
	cdef inline Edge replace(heapdict self, object key, Edge value)
	cpdef tuple popitem(heapdict self)
	cpdef list getheap(heapdict self)
	cpdef Edge getval(heapdict self, Entry entry)

cdef inline list nsmallest(int n, list items)