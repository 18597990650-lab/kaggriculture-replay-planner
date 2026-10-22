"""Public features projected into learned supply coordinates plus retained route detail."""
import math


def embedding(public,day,manifest,vectorize):
    stage='0' if day<12 else '1' if day<21 else '2'
    projection=manifest['projections'][stage]
    raw=vectorize(public,{key:1. for key in manifest['projection_groups']})
    coordinates=tuple(sum(x*w for x,w in zip(raw,row)) for row in projection)
    detail=vectorize(public,manifest['parameters']['weights'])
    scale=math.sqrt(manifest['raw_metric_weight'])
    return coordinates+tuple(v*scale for v in detail)
