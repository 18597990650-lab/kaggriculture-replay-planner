"""Blend public asset-count medians using the already selected route neighbors."""


def validate(model):
    bank=model['asset_counts']
    if model['asset_point_weight']!=.25 or len(bank)!=model['added_count']:
        raise ValueError('Supplementary asset-count contract differs')
    for days in bank:
        if len(days)!=30 or any(len(row)!=8 or any(not isinstance(value,int) or value<0 for value in row) for row in days):
            raise ValueError('Invalid supplementary public asset counts')


def project(model,base_bank,forecast,current,day,selected):
    if forecast is None:
        return
    base_count=model['base_training_count']
    if len(base_bank)!=base_count*30*9:
        raise ValueError('Original asset bank order differs')
    if selected is not None:
        neighbors,weights=selected
        if len(neighbors)!=8 or len(weights)!=8:
            raise ValueError('Asset count projection requires eight neighbors')
    for horizon in (1,3,7):
        target=forecast['horizons'][str(horizon)]
        original=target['median'][:8]
        expanded=list(original)
        if selected is not None:
            future=min(29,day+horizon)
            for product in range(8):
                values=[]
                for index,weight in zip(neighbors,weights):
                    if index<base_count:
                        delta=base_bank[(index*30+future)*9+product]-base_bank[(index*30+day)*9+product]
                    else:
                        days=model['asset_counts'][index-base_count]
                        delta=days[future][product]-days[day][product]
                    values.append((delta,weight))
                cumulative=0.
                for delta,weight in sorted(values):
                    cumulative+=weight
                    if cumulative>=.5:
                        expanded[product]=max(0,current['assets'][product]+delta)
                        break
                else:
                    raise ValueError('Invalid asset neighbor weights')
        target['product_point_estimate']=tuple(old+.25*(new-old) for old,new in zip(original,expanded))
    forecast['product_point_evidence']=dict(method='quarter_supplementary_median',
        products=forecast['columns'][:8],supplementary_weight=.25,
        status='applied' if selected is not None else 'original_median_no_supplementary_match',
        boundary='Continuous crop/animal count points. Existing empirical medians,intervals,land andevent forecasts retain their original meaning. No private query inventory orfuture actions;no executable plan.')
