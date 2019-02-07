import os
import numpy as np
from operator import attrgetter
from six.moves import reduce
from itertools import takewhile

import pandas as pd
import geopandas as gpd
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import cascaded_union

import pycountry as pyc

def _get_country(target, **keys):
    assert len(keys) == 1
    try:
        return getattr(pyc.countries.get(**keys), target)
    except (KeyError, AttributeError):
        return np.nan

def _simplify_polys(polys, minarea=0.1, tolerance=0.01, filterremote=True):
    if isinstance(polys, MultiPolygon):
        polys = sorted(polys, key=attrgetter('area'), reverse=True)
        mainpoly = polys[0]
        mainlength = np.sqrt(mainpoly.area/(2.*np.pi))
        if mainpoly.area > minarea:
            polys = MultiPolygon([p
                                  for p in takewhile(lambda p: p.area > minarea, polys)
                                  if not filterremote or (mainpoly.distance(p) < mainlength)])
        else:
            polys = mainpoly
    return polys.simplify(tolerance=tolerance)

def countries():
    cntries = snakemake.config['countries']
    if 'RS' in cntries: cntries.append('KV')

    df = gpd.read_file(snakemake.input.naturalearth)

    # Names are a hassle in naturalearth, try several fields
    fieldnames = (df[x].where(lambda s: s!='-99') for x in ('ISO_A2', 'WB_A2', 'ADM0_A3'))
    df['name'] = reduce(lambda x,y: x.fillna(y), fieldnames, next(fieldnames)).str[0:2]

    df = df.loc[df.name.isin(cntries) & (df['scalerank'] == 0)]
    s = df.set_index('name')['geometry'].map(_simplify_polys)
    if 'RS' in cntries: s['RS'] = s['RS'].union(s.pop('KV'))

    return s

def eez(country_shapes):
    df = gpd.read_file(snakemake.input.eez)
    df = df.loc[df['ISO_3digit'].isin([_get_country('alpha_3', alpha_2=c) for c in snakemake.config['countries']])]
    df['name'] = df['ISO_3digit'].map(lambda c: _get_country('alpha_2', alpha_3=c))
    s = df.set_index('name').geometry.map(lambda s: _simplify_polys(s, filterremote=False))
    s = gpd.GeoSeries({k:v for k,v in s.iteritems() if v.distance(country_shapes[k]) < 1e-3})
    s.index.name = "name"
    return s

def country_cover(country_shapes, eez_shapes=None):
    shapes = list(country_shapes)
    if eez_shapes is not None:
        shapes += list(eez_shapes)

    europe_shape = cascaded_union(shapes)
    if isinstance(europe_shape, MultiPolygon):
        europe_shape = max(europe_shape, key=attrgetter('area'))
    return Polygon(shell=europe_shape.exterior)

def nuts3(country_shapes):
    df = gpd.read_file(snakemake.input.nuts3)
    df = df.loc[df['STAT_LEVL_'] == 3]
    df['geometry'] = df['geometry'].map(_simplify_polys)
    df = df.rename(columns={'NUTS_ID': 'id'})[['id', 'geometry']].set_index('id')

    pop = pd.read_table(snakemake.input.nuts3pop, na_values=[':'], delimiter=' ?\t', engine='python')
    pop = (pop
           .set_index(pd.MultiIndex.from_tuples(pop.pop('unit,geo\\time').str.split(','))).loc['THS']
           .applymap(lambda x: pd.to_numeric(x, errors='coerce'))
           .fillna(method='bfill', axis=1))['2014']

    gdp = pd.read_table(snakemake.input.nuts3gdp, na_values=[':'], delimiter=' ?\t', engine='python')
    gdp = (gdp
           .set_index(pd.MultiIndex.from_tuples(gdp.pop('unit,geo\\time').str.split(','))).loc['EUR_HAB']
           .applymap(lambda x: pd.to_numeric(x, errors='coerce'))
           .fillna(method='bfill', axis=1))['2014']

    # Swiss data
    cantons = pd.read_csv(snakemake.input.ch_cantons)
    cantons = cantons.set_index(cantons['HASC'].str[3:])['NUTS']
    cantons = cantons.str.pad(5, side='right', fillchar='0')

    swiss = pd.read_excel(snakemake.input.ch_popgdp, skiprows=3, index_col=0)
    swiss.columns = swiss.columns.to_series().map(cantons)

    pop = pop.append(pd.to_numeric(swiss.loc['Residents in 1000', 'CH040':]))
    gdp = gdp.append(pd.to_numeric(swiss.loc['Gross domestic product per capita in Swiss francs', 'CH040':]))

    df = df.join(pd.DataFrame(dict(pop=pop, gdp=gdp)))

    df['country'] = df.index.to_series().str[:2].replace(dict(UK='GB', EL='GR'))

    excludenuts = pd.Index(('FRA10', 'FRA20', 'FRA30', 'FRA40', 'FRA50',
                            'PT200', 'PT300',
                            'ES707', 'ES703', 'ES704','ES705', 'ES706', 'ES708', 'ES709',
                            'FI2', 'FR9'))
    excludecountry = pd.Index(('MT', 'TR', 'LI', 'IS', 'CY', 'KV'))

    df = df.loc[df.index.difference(excludenuts)]
    df = df.loc[~df.country.isin(excludecountry)]

    manual = gpd.GeoDataFrame(
        [['BA1', 'BA', 3871.],
         ['RS1', 'RS', 7210.],
         ['AL1', 'AL', 2893.]],
        columns=['NUTS_ID', 'country', 'pop']
    ).set_index('NUTS_ID')
    manual['geometry'] = manual['country'].map(country_shapes)
    manual = manual.dropna()

    df = df.append(manual)

    df.loc['ME000', 'pop'] = 650.

    return df

def save_to_geojson(df, fn):
    if os.path.exists(fn):
        os.unlink(fn)
    if not isinstance(df, gpd.GeoDataFrame):
        df = gpd.GeoDataFrame(dict(geometry=df))
    df = df.reset_index()
    schema = {**gpd.io.file.infer_schema(df), 'geometry': 'Unknown'}
    df.to_file(fn, driver='GeoJSON', schema=schema)

if __name__ == "__main__":
    # Detect running outside of snakemake and mock snakemake for testing
    if 'snakemake' not in globals():
        from vresutils.snakemake import MockSnakemake, Dict
        snakemake = MockSnakemake(
            path='..',
            wildcards={},
            input=Dict(
                naturalearth='data/bundle/naturalearth/ne_10m_admin_0_countries.shp',
                eez='data/bundle/eez/World_EEZ_v8_2014.shp',
                nuts3='data/bundle/NUTS_2013_60M_SH/data/NUTS_RG_60M_2013.shp',
                nuts3pop='data/bundle/nama_10r_3popgdp.tsv.gz',
                nuts3gdp='data/bundle/nama_10r_3gdp.tsv.gz',
                ch_cantons='data/bundle/ch_cantons.csv',
                ch_popgdp='data/bundle/je-e-21.03.02.xls'
            ),
            output=Dict(
                country_shapes='resources/country_shapes.geojson',
                offshore_shapes='resource/offshore_shapes.geojson',
                europe_shape='resources/europe_shape.geojson',
                nuts3_shapes='resources/nuts3_shapes.geojson'
            )
        )

    country_shapes = countries()
    save_to_geojson(country_shapes, snakemake.output.country_shapes)

    offshore_shapes = eez(country_shapes)
    save_to_geojson(offshore_shapes, snakemake.output.offshore_shapes)

    europe_shape = country_cover(country_shapes, offshore_shapes)
    save_to_geojson(gpd.GeoSeries(europe_shape), snakemake.output.europe_shape)

    nuts3_shapes = nuts3(country_shapes)
    save_to_geojson(nuts3_shapes, snakemake.output.nuts3_shapes)
