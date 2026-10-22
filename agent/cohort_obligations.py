"""Identify observed unfulfilled same-day animal establishment obligations."""
def same_day_establishments(observation,baseline,result):
    day=observation['step']//24
    own=observation['farms'][observation['player']]
    expected={}
    for state in baseline['day_states']:
        for y,row in enumerate(state['farm']['tiles']):
            for x,t in enumerate(row):
                old=own['tiles'][y][x]
                if isinstance(t,dict) and t.get('animal') and t.get('placed_day')==day and not (
                    isinstance(old,dict) and old.get('animal')==t['animal'] and old.get('placed_day')==day):
                    expected[(x,y)]=(t['animal'],day)
    actual={}
    for state in result['day_states']:
        for p in expected:
            tile=state['farm']['tiles'][p[1]][p[0]]
            if isinstance(tile,dict) and tile.get('animal')==expected[p][0]:
                actual.setdefault(p,tile['placed_day'])
    return [dict(point=p,item=identity[0],expected_day=day,actual_day=actual.get(p))
        for p,identity in expected.items() if actual.get(p,99)>day]
