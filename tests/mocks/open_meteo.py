"""Mock of the Open-Meteo geocoding API (geocoding-api.open-meteo.com/v1/search) and forecast API
(api.open-meteo.com/v1/forecast). Both are free and keyless, so there is no auth to check.

Real behaviour reproduced:
  * geocoding matches the place NAME only: 1 character returns nothing, 2 characters must match exactly,
    3 or more do a case and accent insensitive prefix match. A qualifier after a comma must match exactly
    (no prefixes): a country name, a 2-letter country code, an admin1 (state) name or an admin1 abbreviation,
    so "Austin, Texas" and "Austin, TX" work and "Austin, Tex" does not. Admin1 abbreviations win over country
    codes ("Portland, CA" means California, not Canada).
  * no match: HTTP 200 with no "results" key at all
  * forecast: latitude and longitude required and in range, forecast_days 0 to 16 (default 7), daily
    variables must be real variable names, daily data needs a timezone ("auto" is the usual choice)
  * errors are HTTP 400 {"error": true, "reason": "..."}
  * the forecast's first day is "today" in the location's timezone, from the mock clock
"""
import unicodedata
import zlib
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import json_response

DAILY_VARIABLES = {
    'temperature_2m_max': '°C', 'temperature_2m_min': '°C', 'apparent_temperature_max': '°C',
    'apparent_temperature_min': '°C', 'precipitation_sum': 'mm', 'precipitation_probability_max': '%',
    'weather_code': 'wmo code', 'wind_speed_10m_max': 'km/h', 'uv_index_max': '', 'sunrise': 'iso8601',
    'sunset': 'iso8601', 'rain_sum': 'mm', 'daylight_duration': 's',
}

# Fictional forecasts for real city coordinates. temps: daily maximum in Celsius for 16 days.
CITIES = [
    dict(id=5308655, name='Phoenix', latitude=33.44838, longitude=-112.07404, elevation=331.0, feature_code='PPLA',
         country_code='US', admin1='Arizona', timezone='America/Phoenix', population=1680992, country='United States',
         temps=[36.4, 37.1, 35.8, 34.9, 33.2, 32.5, 31.8, 31.0, 30.2, 29.9, 29.5, 29.1, 28.8, 28.4, 28.0, 27.6]),
    dict(id=5318313, name='Tucson', latitude=32.22174, longitude=-110.92648, elevation=728.0, feature_code='PPLA2',
         country_code='US', admin1='Arizona', timezone='America/Phoenix', population=548073, country='United States',
         temps=[33.9, 34.6, 33.0, 32.1, 31.5, 30.8, 30.0, 29.4, 28.9, 28.1, 27.7, 27.0, 26.6, 26.0, 25.5, 25.1]),
    dict(id=4671654, name='Austin', latitude=30.26715, longitude=-97.74306, elevation=149.0, feature_code='PPLA',
         country_code='US', admin1='Texas', timezone='America/Chicago', population=961855, country='United States',
         temps=[29.2, 30.1, 28.4, 27.0, 26.3, 25.8, 25.0, 24.6, 24.1, 23.8, 23.0, 22.5, 22.1, 21.8, 21.0, 20.5]),
    dict(id=4699066, name='Houston', latitude=29.76328, longitude=-95.36327, elevation=14.0, feature_code='PPLA2',
         country_code='US', admin1='Texas', timezone='America/Chicago', population=2304580, country='United States',
         temps=[30.5, 31.2, 30.0, 29.1, 28.4, 27.9, 27.3, 26.8, 26.0, 25.5, 25.1, 24.7, 24.0, 23.6, 23.1, 22.8]),
    dict(id=4164138, name='Miami', latitude=25.77427, longitude=-80.19366, elevation=2.0, feature_code='PPLA2',
         country_code='US', admin1='Florida', timezone='America/New_York', population=442241, country='United States',
         temps=[31.0, 30.6, 30.9, 31.3, 30.2, 29.8, 29.5, 29.9, 30.1, 29.4, 29.0, 28.8, 28.5, 28.3, 28.0, 27.9]),
    dict(id=4167147, name='Orlando', latitude=28.53834, longitude=-81.37924, elevation=32.0, feature_code='PPLA2',
         country_code='US', admin1='Florida', timezone='America/New_York', population=307573, country='United States',
         temps=[29.4, 28.9, 29.6, 28.1, 27.5, 27.0, 26.8, 26.2, 25.9, 25.4, 25.0, 24.6, 24.2, 23.9, 23.5, 23.0]),
    dict(id=5506956, name='Las Vegas', latitude=36.17497, longitude=-115.13722, elevation=613.0, feature_code='PPLA2',
         country_code='US', admin1='Nevada', timezone='America/Los_Angeles', population=641903, country='United States',
         temps=[30.1, 31.0, 29.5, 28.0, 27.2, 26.5, 25.9, 25.1, 24.4, 23.8, 23.0, 22.4, 21.9, 21.2, 20.8, 20.1]),
    dict(id=5391811, name='San Diego', latitude=32.71571, longitude=-117.16472, elevation=20.0, feature_code='PPLA2',
         country_code='US', admin1='California', timezone='America/Los_Angeles', population=1394928, country='United States',
         temps=[24.1, 24.6, 25.0, 24.3, 23.8, 23.4, 23.0, 22.8, 22.5, 22.1, 21.9, 21.6, 21.2, 21.0, 20.8, 20.5]),
    dict(id=4180439, name='Atlanta', latitude=33.749, longitude=-84.38798, elevation=320.0, feature_code='PPLA',
         country_code='US', admin1='Georgia', timezone='America/New_York', population=498715, country='United States',
         temps=[24.8, 25.6, 26.1, 23.9, 22.4, 21.8, 21.0, 20.5, 19.9, 19.4, 18.8, 18.1, 17.6, 17.0, 16.5, 16.0]),
    dict(id=4644585, name='Nashville', latitude=36.16589, longitude=-86.78444, elevation=169.0, feature_code='PPLA',
         country_code='US', admin1='Tennessee', timezone='America/Chicago', population=715884, country='United States',
         temps=[22.5, 23.1, 21.4, 20.0, 19.2, 18.8, 18.0, 17.5, 17.0, 16.4, 15.9, 15.3, 14.8, 14.2, 13.9, 13.1]),
    dict(id=5419384, name='Denver', latitude=39.73915, longitude=-104.9847, elevation=1609.0, feature_code='PPLA',
         country_code='US', admin1='Colorado', timezone='America/Denver', population=715522, country='United States',
         temps=[17.2, 18.4, 15.9, 14.1, 13.0, 12.6, 12.0, 11.5, 11.1, 10.4, 9.9, 9.2, 8.8, 8.1, 7.6, 7.0]),
    dict(id=5780993, name='Salt Lake City', latitude=40.76078, longitude=-111.89105, elevation=1288.0, feature_code='PPLA',
         country_code='US', admin1='Utah', timezone='America/Denver', population=200133, country='United States',
         temps=[16.0, 16.8, 15.1, 13.9, 12.8, 12.1, 11.6, 11.0, 10.4, 9.9, 9.3, 8.8, 8.2, 7.9, 7.2, 6.8]),
    dict(id=5809844, name='Seattle', latitude=47.60621, longitude=-122.33207, elevation=56.0, feature_code='PPLA2',
         country_code='US', admin1='Washington', timezone='America/Los_Angeles', population=737015, country='United States',
         temps=[13.2, 12.8, 12.1, 11.9, 12.4, 11.6, 11.0, 10.8, 10.4, 10.0, 9.7, 9.5, 9.1, 8.9, 8.6, 8.2]),
    dict(id=5746545, name='Portland', latitude=45.52345, longitude=-122.67621, elevation=15.0, feature_code='PPLA2',
         country_code='US', admin1='Oregon', timezone='America/Los_Angeles', population=652503, country='United States',
         temps=[14.1, 13.5, 13.0, 12.6, 12.9, 12.2, 11.8, 11.3, 10.9, 10.5, 10.0, 9.8, 9.4, 9.0, 8.7, 8.4]),
    dict(id=4975802, name='Portland', latitude=43.66147, longitude=-70.25533, elevation=21.0, feature_code='PPLA2',
         country_code='US', admin1='Maine', timezone='America/New_York', population=68408, country='United States',
         temps=[11.0, 10.4, 9.8, 9.1, 8.6, 8.0, 7.5, 7.1, 6.6, 6.0, 5.5, 5.1, 4.6, 4.2, 3.9, 3.5]),
    dict(id=4887398, name='Chicago', latitude=41.85003, longitude=-87.65005, elevation=181.0, feature_code='PPLA2',
         country_code='US', admin1='Illinois', timezone='America/Chicago', population=2746388, country='United States',
         temps=[12.3, 11.1, 13.4, 10.2, 9.5, 9.0, 8.4, 7.9, 7.2, 6.8, 6.1, 5.5, 5.0, 4.4, 3.9, 3.2]),
    dict(id=5037649, name='Minneapolis', latitude=44.97997, longitude=-93.26384, elevation=264.0, feature_code='PPLA2',
         country_code='US', admin1='Minnesota', timezone='America/Chicago', population=429954, country='United States',
         temps=[8.9, 7.6, 9.4, 6.5, 5.8, 5.0, 4.4, 3.8, 3.1, 2.5, 1.9, 1.2, 0.6, 0.1, -0.5, -1.0]),
    dict(id=5261457, name='Madison', latitude=43.07305, longitude=-89.40123, elevation=270.0, feature_code='PPLA',
         country_code='US', admin1='Wisconsin', timezone='America/Chicago', population=269840, country='United States',
         temps=[10.2, 9.1, 10.8, 8.0, 7.4, 6.9, 6.2, 5.6, 5.0, 4.5, 3.8, 3.2, 2.7, 2.1, 1.6, 1.0]),
    dict(id=4930956, name='Boston', latitude=42.35843, longitude=-71.05977, elevation=38.0, feature_code='PPLA',
         country_code='US', admin1='Massachusetts', timezone='America/New_York', population=675647, country='United States',
         temps=[14.6, 13.2, 12.8, 15.1, 12.0, 11.4, 10.8, 10.3, 9.7, 9.2, 8.6, 8.0, 7.5, 7.1, 6.5, 6.0]),
    dict(id=5110302, name='Brooklyn', latitude=40.6501, longitude=-73.94958, elevation=10.0, feature_code='PPLA2',
         country_code='US', admin1='New York', timezone='America/New_York', population=2736074, country='United States',
         temps=[16.1, 15.4, 14.9, 16.8, 14.0, 13.5, 13.0, 12.4, 11.8, 11.3, 10.7, 10.1, 9.6, 9.0, 8.5, 8.0]),
    dict(id=5234372, name='Burlington', latitude=44.47588, longitude=-73.21207, elevation=61.0, feature_code='PPLA2',
         country_code='US', admin1='Vermont', timezone='America/New_York', population=44743, country='United States',
         temps=[9.4, 8.6, 8.1, 9.8, 7.0, 6.4, 5.8, 5.3, 4.7, 4.1, 3.6, 3.0, 2.5, 1.9, 1.4, 0.8]),
    dict(id=5780026, name='Honolulu', latitude=21.30694, longitude=-157.85833, elevation=18.0, feature_code='PPLA',
         country_code='US', admin1='Hawaii', timezone='Pacific/Honolulu', population=345064, country='United States',
         temps=[29.8, 30.0, 29.6, 29.9, 30.2, 29.7, 29.5, 29.8, 29.4, 29.3, 29.6, 29.2, 29.0, 29.1, 28.9, 28.8]),
]
DEFAULT_TEMPS = [18.0] * 16
# admin1 abbreviations accepted after the comma ("Austin, TX"); they win over a 2-letter country code
ADMIN1_CODES = {'Arizona': 'AZ', 'Texas': 'TX', 'Florida': 'FL', 'Nevada': 'NV', 'California': 'CA', 'Georgia': 'GA',
                'Tennessee': 'TN', 'Colorado': 'CO', 'Utah': 'UT', 'Washington': 'WA', 'Oregon': 'OR', 'Maine': 'ME',
                'Illinois': 'IL', 'Minnesota': 'MN', 'Wisconsin': 'WI', 'Massachusetts': 'MA', 'New York': 'NY',
                'Vermont': 'VT', 'Hawaii': 'HI'}


def _fold(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text) if not unicodedata.combining(c)).lower().strip()


def _err(reason):
    return json_response(400, {'error': True, 'reason': reason})


class OpenMeteoMock:
    def __init__(self, server):
        self.server = server
        self.reset()

    def reset(self):
        self.cities = [dict(c, admin1_id=zlib.crc32(c['admin1'].encode()) % 10_000_000, country_id=6252001, temps=list(c['temps']),
                            admin1_code=ADMIN1_CODES.get(c['admin1'], ''))
                       for c in CITIES]
        self.forecasts = {}

    # ------------------------------------------------------------------ test helpers

    def add_city(self, name, latitude, longitude, temps=None, **extra):
        admin1 = extra.pop('admin1', '')
        city = dict(id=9_000_000 + len(self.cities), name=name, latitude=latitude, longitude=longitude, elevation=extra.pop('elevation', 10.0),
                    feature_code='PPL', country_code=extra.pop('country_code', 'US'), admin1=admin1,
                    admin1_code=extra.pop('admin1_code', ADMIN1_CODES.get(admin1, '')),
                    timezone=extra.pop('timezone', 'America/New_York'), population=extra.pop('population', 10000),
                    country=extra.pop('country', 'United States'), admin1_id=1, country_id=6252001,
                    temps=list(temps or DEFAULT_TEMPS), **extra)
        self.cities.append(city)
        return city

    def set_forecast(self, city, temps):
        """Override the daily max temperatures (Celsius, first value is today) for a city name or (lat, lon)."""
        targets = [c for c in self.cities if c['name'] == city] if isinstance(city, str) else \
            [c for c in self.cities if abs(c['latitude'] - city[0]) < 0.05 and abs(c['longitude'] - city[1]) < 0.05]
        if not targets:
            raise KeyError(city)
        for c in targets:
            c['temps'] = list(temps) + [temps[-1]] * (16 - len(temps))

    # ------------------------------------------------------------------ routing

    def route(self, path):
        if path == '/v1/search':
            return 'open-meteo-geocoding'
        if path == '/v1/forecast':
            return 'open-meteo-forecast'
        return None

    def annotate(self, req, entry):
        entry['params'] = {k: v[-1] for k, v in req.query.items()}

    def error_body(self, status):
        return {'error': True, 'reason': 'Internal server error' if status >= 500 else 'Bad request'}

    def handle(self, req, entry):
        if req.method != 'GET':
            return _err('Only GET requests are supported')
        if req.path == '/v1/search':
            return self._geocode(req)
        return self._forecast(req)

    # ------------------------------------------------------------------ geocoding

    def _geocode(self, req):
        name = req.q('name')
        if name is None:
            return _err("Parameter 'name' is required")
        try:
            count = int(req.q('count', '10'))
        except ValueError:
            return _err("Value of type 'Int' required for key 'count'.")
        if not 1 <= count <= 100:
            return _err('Count must be between 1 and 100')
        fmt = req.q('format', 'json')
        if fmt not in ('json', 'protobuf'):
            return _err("Cannot initialize Format from invalid String value " + fmt)
        country = req.q('countryCode')
        base, _, qualifier = name.partition(',')
        q = _fold(base)
        qual = _fold(qualifier)
        hits = []
        is_admin1_code = bool(qual) and any(_fold(c.get('admin1_code', '')) == qual for c in self.cities)
        if len(q) >= 2:
            for c in self.cities:
                n = _fold(c['name'])
                ok = n == q if len(q) == 2 else n.startswith(q)
                if not ok:
                    continue
                if country and c['country_code'] != country.upper():
                    continue
                if qual:
                    if is_admin1_code:
                        if _fold(c.get('admin1_code', '')) != qual:
                            continue
                    elif qual not in (_fold(c['country']), _fold(c['country_code']), _fold(c['admin1'])):
                        continue
                hits.append(c)
        hits.sort(key=lambda c: -c['population'])
        out = {'generationtime_ms': 0.4}
        if hits:
            out = {'results': [self._result(c) for c in hits[:count]], 'generationtime_ms': 0.4}
        return json_response(200, out)

    @staticmethod
    def _result(c):
        keys = ('id', 'name', 'latitude', 'longitude', 'elevation', 'feature_code', 'country_code', 'admin1_id', 'timezone',
                'population', 'country_id', 'country', 'admin1')
        return {k: c[k] for k in keys if c.get(k) not in (None, '')}

    # ------------------------------------------------------------------ forecast

    def _forecast(self, req):
        try:
            lat = float(req.q('latitude'))
        except (TypeError, ValueError):
            return _err("Value of type 'Float' required for key 'latitude'.")
        try:
            lon = float(req.q('longitude'))
        except (TypeError, ValueError):
            return _err("Value of type 'Float' required for key 'longitude'.")
        if not -90 <= lat <= 90:
            return _err(f'Latitude must be in range of -90 to 90°. Given: {lat}.')
        if not -180 <= lon <= 180:
            return _err(f'Longitude must be in range of -180 to 180°. Given: {lon}.')
        try:
            days = int(req.q('forecast_days', '7'))
        except ValueError:
            return _err("Value of type 'Int' required for key 'forecast_days'.")
        if not 0 <= days <= 16:
            return _err('Forecast days is invalid. Allowed range 0 to 16.')
        daily = []
        for v in req.query.get('daily', []):
            daily += [x for x in v.split(',') if x]
        for v in daily:
            if v not in DAILY_VARIABLES:
                return _err(f'Cannot initialize ForecastVariableDaily from invalid String value {v} for key daily')
        unit = req.q('temperature_unit', 'celsius')
        if unit not in ('celsius', 'fahrenheit'):
            return _err(f'Cannot initialize TemperatureUnit from invalid String value {unit}')
        tz_param = req.q('timezone')
        if daily and not tz_param:
            return _err("Parameter 'timezone' is required when daily variables are requested. Use 'auto' to resolve it from the coordinates.")
        city = min(self.cities, key=lambda c: (c['latitude'] - lat) ** 2 + (c['longitude'] - lon) ** 2)
        if (city['latitude'] - lat) ** 2 + (city['longitude'] - lon) ** 2 > 0.25:
            city = dict(name='unknown', timezone='GMT', elevation=10.0, temps=DEFAULT_TEMPS, latitude=lat, longitude=lon)
        tz_name = city['timezone'] if tz_param == 'auto' else (tz_param or 'GMT')
        try:
            zone = ZoneInfo('UTC' if tz_name in ('GMT', 'UTC') else tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            return _err(f'Invalid timezone {tz_name}')
        local_now = self.server.clock.now().astimezone(zone)
        start = local_now.date()
        if req.q('start_date') or req.q('end_date'):
            if not (req.q('start_date') and req.q('end_date')):
                return _err("Parameter 'start_date' and 'end_date' must both be set")
            from datetime import date
            try:
                s, e = date.fromisoformat(req.q('start_date')), date.fromisoformat(req.q('end_date'))
            except ValueError:
                return _err('Invalid date format. Use yyyy-mm-dd')
            offset, days = (s - start).days, (e - s).days + 1
            if offset < 0 or offset + days > 16:
                return _err('Parameter start_date or end_date is out of the allowed range')
        else:
            offset = 0
        temps = city['temps'][offset:offset + days]
        dates = [(start + timedelta(days=offset + i)).isoformat() for i in range(days)]
        offset_secs = int(local_now.utcoffset().total_seconds())
        out = {'latitude': round(lat, 4), 'longitude': round(lon, 4), 'generationtime_ms': 0.2,
               'utc_offset_seconds': offset_secs, 'timezone': tz_name,
               'timezone_abbreviation': local_now.tzname() if tz_name not in ('GMT', 'UTC') else 'GMT',
               'elevation': city['elevation']}
        if daily:
            conv = (lambda c: round(c * 9 / 5 + 32, 1)) if unit == 'fahrenheit' else (lambda c: c)
            units = {'time': 'iso8601'}
            data = {'time': dates}
            for v in daily:
                u = DAILY_VARIABLES[v]
                units[v] = '°F' if u == '°C' and unit == 'fahrenheit' else u
                if v in ('temperature_2m_max', 'apparent_temperature_max'):
                    data[v] = [conv(t + (1.5 if v.startswith('apparent') else 0)) for t in temps]
                elif v in ('temperature_2m_min', 'apparent_temperature_min'):
                    data[v] = [conv(round(t - 9.0, 1)) for t in temps]
                elif v in ('sunrise', 'sunset'):
                    data[v] = [f'{d}T{"06:58" if v == "sunrise" else "18:31"}' for d in dates]
                elif v == 'weather_code':
                    data[v] = [1 for _ in dates]
                else:
                    data[v] = [0.0 for _ in dates]
            out['daily_units'] = units
            out['daily'] = data
        return json_response(200, out)
