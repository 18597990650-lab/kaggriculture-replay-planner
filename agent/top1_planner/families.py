"""Choose an expansion family from the shops currently visible at its entry."""
from . import rules


def family_key(observation):
    shops=observation['town']['unlocked_shops']
    dairy=sum('MILK' in rules.SHOPS[shop] for shop in shops)
    yarn=any('WOOL' in rules.SHOPS[shop] for shop in shops)
    return f'dairy={dairy};yarn={yarn}'


def family_bases(observation,programmes,families):
    key=family_key(observation)
    names=families[key]
    indexed={programme.name:programme for programme in programmes}
    return key,[indexed[name] for name in names]
