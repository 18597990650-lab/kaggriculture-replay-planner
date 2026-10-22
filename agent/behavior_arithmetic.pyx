# cython: language_level=3, boundscheck=False, wraparound=False
"""Preserve the eight-term Python sum order while removing generator overhead."""


def weighted_targets(rows, weights):
    cdef Py_ssize_t width, i, j
    cdef double coefficients[8]
    cdef double value
    cdef list output
    if len(rows)!=8 or len(weights)!=8:
        raise ValueError('Exactly eight neighbor rows and weights are required')
    width=len(rows[0])
    for j in range(8):
        if len(rows[j])!=width:
            raise ValueError('Neighbor target widths differ')
        coefficients[j]=weights[j]
    output=[]
    for i in range(width):
        value=0.
        for j in range(8):
            value=value+coefficients[j]*<double>rows[j][i]
        output.append(value)
    return tuple(output)
