"""A strict subset of the Airtable formula language, used by the mock for filterByFormula.

Parsing follows Airtable's syntax: {Field Name} references (or bare names without spaces), single or
double quoted strings, numbers, the operators = != < > <= >= & + - * /, and function calls.
An unknown function or a syntax problem raises FormulaSyntaxError, an unknown field raises
UnknownFieldsError. The mock turns both into Airtable's 422 INVALID_FILTER_BY_FORMULA.

Value rules that match Airtable and matter for filters:
  * text comparison with = is case sensitive (use LOWER() or UPPER() to compare loosely)
  * BLANK() equals an empty value, an empty string, 0 and an unchecked checkbox
  * a linked record field evaluates to the PRIMARY FIELD VALUES of the linked records, not their ids,
    so ARRAYJOIN({Customer}) gives "Maya Chen" and never "rec..."
  * a multiple select or link field used as text is joined with ", "
  * FIND returns 0 when not found, SEARCH returns BLANK. Both are treated as case sensitive: Airtable's
    reference does not promise that SEARCH ignores case, so wrap both sides in LOWER() to match loosely
  * DATETIME_DIFF(a, b, unit) is a minus b, truncated to a whole number
  * TODAY() and NOW() come from the mock clock (UTC)
  * an error while evaluating one record (for example text plus a number) excludes that record
"""
import calendar
import math
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

__all__ = ['FormulaSyntaxError', 'UnknownFieldsError', 'BLANK', 'Arr', 'DT', 'parse', 'evaluate', 'matches',
           'parse_datetime', 'field_refs']


class FormulaSyntaxError(Exception):
    pass


class UnknownFieldsError(Exception):
    def __init__(self, names):
        super().__init__(', '.join(names))
        self.names = names


class FormulaEvalError(Exception):
    pass


class _Blank:
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __bool__(self):
        return False

    def __repr__(self):
        return 'BLANK'


BLANK = _Blank()


class Arr(list):
    """An array value (multiple select, linked records, lookups)."""


class DT:
    """A date or datetime value. Always timezone aware (UTC)."""
    __slots__ = ('dt', 'date_only')

    def __init__(self, dt, date_only=False):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        self.dt = dt.astimezone(timezone.utc)
        self.date_only = date_only

    def __repr__(self):
        return f'DT({self.iso()})'

    def iso(self):
        if self.date_only:
            return self.dt.strftime('%Y-%m-%d')
        return self.dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{self.dt.microsecond // 1000:03d}Z'


# ---------------------------------------------------------------------------------------------- dates

_ISO = re.compile(r'^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?\s*(Z|[+-]\d{2}:?\d{2})?)?$')
_US = re.compile(r'^(\d{1,2})/(\d{1,2})/(\d{4})$')


def parse_datetime(text):
    """Parse an ISO 8601 date or datetime (and M/D/YYYY). Returns DT or None."""
    if isinstance(text, DT):
        return text
    if not isinstance(text, str):
        return None
    s = text.strip()
    m = _ISO.match(s)
    if m:
        y, mo, d, hh, mi, ss, frac, tz = m.groups()
        try:
            if hh is None:
                return DT(datetime(int(y), int(mo), int(d), tzinfo=timezone.utc), date_only=True)
            micro = int((frac or '0').ljust(6, '0')[:6])
            dt = datetime(int(y), int(mo), int(d), int(hh), int(mi), int(ss or 0), micro)
            if tz in (None, 'Z'):
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                sign = 1 if tz[0] == '+' else -1
                digits = tz[1:].replace(':', '')
                dt = dt.replace(tzinfo=timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))))
            return DT(dt)
        except ValueError:
            return None
    m = _US.match(s)
    if m:
        mo, d, y = m.groups()
        try:
            return DT(datetime(int(y), int(mo), int(d), tzinfo=timezone.utc), date_only=True)
        except ValueError:
            return None
    return None


_UNITS = {
    'ms': 'milliseconds', 'millisecond': 'milliseconds', 'milliseconds': 'milliseconds',
    's': 'seconds', 'second': 'seconds', 'seconds': 'seconds',
    'm': 'minutes', 'minute': 'minutes', 'minutes': 'minutes',
    'h': 'hours', 'hour': 'hours', 'hours': 'hours',
    'd': 'days', 'day': 'days', 'days': 'days',
    'w': 'weeks', 'week': 'weeks', 'weeks': 'weeks',
    'M': 'months', 'month': 'months', 'months': 'months',
    'Q': 'quarters', 'quarter': 'quarters', 'quarters': 'quarters',
    'y': 'years', 'year': 'years', 'years': 'years',
}


def _unit(u):
    if u is BLANK:
        return 'seconds'
    key = str(u)
    if key in _UNITS:
        return _UNITS[key]
    if key.lower() in _UNITS and key not in ('M',):
        return _UNITS[key.lower()]
    raise FormulaEvalError(f'unknown unit {u!r}')


def _add_months(dt, months):
    total = dt.month - 1 + months
    year, month = dt.year + total // 12, total % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _month_diff(a, b):
    months = (a.year - b.year) * 12 + (a.month - b.month)
    # truncate toward zero like moment.js
    anchor = _add_months(b, months)
    if months > 0 and anchor > a:
        months -= 1
    elif months < 0 and anchor < a:
        months += 1
    return months


def _truncate(dt, unit):
    if unit == 'years':
        return dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if unit == 'quarters':
        return dt.replace(month=(dt.month - 1) // 3 * 3 + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if unit == 'months':
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if unit == 'weeks':
        start = dt - timedelta(days=(dt.weekday() + 1) % 7)  # weeks start on Sunday
        return start.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == 'days':
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == 'hours':
        return dt.replace(minute=0, second=0, microsecond=0)
    if unit == 'minutes':
        return dt.replace(second=0, microsecond=0)
    if unit == 'seconds':
        return dt.replace(microsecond=0)
    return dt


# moment.js format tokens (Airtable's DATETIME_FORMAT uses them), longest first. Letters that are not tokens are
# copied as they are, like moment does; [text] is literal text.
_FMT_TOKENS = re.compile(r'\[[^\]]*\]|YYYY|GGGG|YY|MMMM|MMM|MM|M|Do|DDDD|DDD|DD|D|dddd|ddd|dd|d|E|e|WW|W|ww|w|Q|'
                         r'HH|H|hh|h|mm|m|ss|s|SSS|A|a|ZZ|Z|x|X')


def _ordinal(n):
    suffix = 'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f'{n}{suffix}'


def _locale_week(d):
    """moment's en locale week: weeks start on Sunday and week 1 is the week that contains January 1."""
    jan1 = d.replace(month=1, day=1)
    offset = (jan1.weekday() + 1) % 7                      # days from the Sunday that starts week 1 to Jan 1
    return (d.timetuple().tm_yday - 1 + offset) // 7 + 1


def _format(dt_value, fmt):
    d = dt_value.dt
    iso_year, iso_week, iso_day = d.isocalendar()

    def rep(m):
        t = m.group(0)
        if t.startswith('['):
            return t[1:-1]
        return {
            'YYYY': f'{d.year:04d}', 'YY': f'{d.year % 100:02d}', 'GGGG': f'{iso_year:04d}',
            'MMMM': calendar.month_name[d.month], 'MMM': calendar.month_abbr[d.month], 'MM': f'{d.month:02d}',
            'M': str(d.month), 'Q': str((d.month - 1) // 3 + 1),
            'Do': _ordinal(d.day), 'DDDD': f'{d.timetuple().tm_yday:03d}', 'DDD': str(d.timetuple().tm_yday),
            'DD': f'{d.day:02d}', 'D': str(d.day),
            'dddd': calendar.day_name[d.weekday()], 'ddd': calendar.day_abbr[d.weekday()],
            'dd': calendar.day_abbr[d.weekday()][:2], 'd': str((d.weekday() + 1) % 7), 'E': str(iso_day),
            'e': str((d.weekday() + 1) % 7),
            'WW': f'{iso_week:02d}', 'W': str(iso_week), 'ww': f'{_locale_week(d):02d}', 'w': str(_locale_week(d)),
            'HH': f'{d.hour:02d}', 'H': str(d.hour), 'hh': f'{(d.hour % 12) or 12:02d}', 'h': str((d.hour % 12) or 12),
            'mm': f'{d.minute:02d}', 'm': str(d.minute), 'ss': f'{d.second:02d}', 's': str(d.second),
            'SSS': f'{d.microsecond // 1000:03d}',
            'A': 'AM' if d.hour < 12 else 'PM', 'a': 'am' if d.hour < 12 else 'pm', 'ZZ': '+0000', 'Z': '+00:00',
            'x': str(int(d.timestamp() * 1000)), 'X': str(int(d.timestamp())),
        }[t]
    return _FMT_TOKENS.sub(rep, fmt)


def _weekday(d, start=None):
    """WEEKDAY: 0 to 6, weeks start on Sunday unless the second argument is "Monday"."""
    dv = _dt(d)
    if dv is None:
        return BLANK
    if start is not None and start is not BLANK and to_str(start).strip().lower() == 'monday':
        return dv.dt.weekday()
    return (dv.dt.weekday() + 1) % 7


def _weeknum(d, start=None):
    """WEEKNUM: week 1 contains January 1; weeks start on Sunday unless the second argument is "Monday"."""
    dv = _dt(d)
    if dv is None:
        return BLANK
    monday = start is not None and start is not BLANK and to_str(start).strip().lower() == 'monday'
    jan1 = dv.dt.replace(month=1, day=1)
    offset = jan1.weekday() if monday else (jan1.weekday() + 1) % 7
    return (dv.dt.timetuple().tm_yday - 1 + offset) // 7 + 1


# ---------------------------------------------------------------------------------------------- lexer

_TOKEN = re.compile(r'''
    (?P<ws>\s+)
  | (?P<field>\{(?:[^}\\]|\\.)*\})
  | (?P<num>\d+\.\d*|\.\d+|\d+)
  | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<op>!=|<=|>=|=|<|>|&|\+|-|\*|/|\(|\)|,)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
''', re.VERBOSE)

_ESC = {'n': '\n', 't': '\t', 'r': '\r', '\\': '\\', '"': '"', "'": "'"}


def _unescape(body):
    out, i = [], 0
    while i < len(body):
        c = body[i]
        if c == '\\' and i + 1 < len(body):
            out.append(_ESC.get(body[i + 1], body[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def _tokens(text):
    pos, out = 0, []
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise FormulaSyntaxError(f'unexpected character {text[pos]!r} at {pos}')
        kind = m.lastgroup
        val = m.group(kind)
        pos = m.end()
        if kind == 'ws':
            continue
        if kind == 'field':
            out.append(('field', _unescape(val[1:-1])))
        elif kind == 'num':
            out.append(('num', float(val) if '.' in val else int(val)))
        elif kind == 'str':
            out.append(('str', _unescape(val[1:-1])))
        else:
            out.append((kind, val))
    out.append(('eof', None))
    return out


# ---------------------------------------------------------------------------------------------- parser

class _Parser:
    def __init__(self, text):
        self.toks = _tokens(text)
        self.i = 0

    def peek(self):
        return self.toks[self.i]

    def take(self, kind=None, val=None):
        tok = self.toks[self.i]
        if kind and (tok[0] != kind or (val is not None and tok[1] != val)):
            raise FormulaSyntaxError(f'expected {val or kind}, got {tok[1]!r}')
        self.i += 1
        return tok

    def parse(self):
        node = self.comparison()
        if self.peek()[0] != 'eof':
            raise FormulaSyntaxError(f'unexpected {self.peek()[1]!r}')
        return node

    def comparison(self):
        left = self.concat()
        while self.peek()[0] == 'op' and self.peek()[1] in ('=', '!=', '<', '>', '<=', '>='):
            op = self.take()[1]
            left = ('bin', op, left, self.concat())
        return left

    def concat(self):
        left = self.additive()
        while self.peek() == ('op', '&'):
            self.take()
            left = ('bin', '&', left, self.additive())
        return left

    def additive(self):
        left = self.term()
        while self.peek()[0] == 'op' and self.peek()[1] in ('+', '-'):
            op = self.take()[1]
            left = ('bin', op, left, self.term())
        return left

    def term(self):
        left = self.unary()
        while self.peek()[0] == 'op' and self.peek()[1] in ('*', '/'):
            op = self.take()[1]
            left = ('bin', op, left, self.unary())
        return left

    def unary(self):
        if self.peek() == ('op', '-'):
            self.take()
            return ('neg', self.unary())
        return self.primary()

    def primary(self):
        kind, val = self.peek()
        if kind == 'num':
            self.take()
            return ('lit', val)
        if kind == 'str':
            self.take()
            return ('lit', val)
        if kind == 'field':
            self.take()
            return ('field', val)
        if kind == 'op' and val == '(':
            self.take()
            node = self.comparison()
            self.take('op', ')')
            return node
        if kind == 'name':
            self.take()
            if self.peek() == ('op', '('):
                self.take()
                args = []
                if self.peek() != ('op', ')'):
                    args.append(self.comparison())
                    while self.peek() == ('op', ','):
                        self.take()
                        args.append(self.comparison())
                self.take('op', ')')
                fname = val.upper()
                if fname not in FUNCTIONS:
                    raise FormulaSyntaxError(f'unknown function {val}')
                lo, hi, _ = FUNCTIONS[fname]
                if len(args) < lo or (hi is not None and len(args) > hi):
                    raise FormulaSyntaxError(f'{fname} takes {lo} to {hi} arguments, got {len(args)}')
                return ('call', fname, args)
            if val.upper() in ('TRUE', 'FALSE'):
                return ('lit', val.upper() == 'TRUE')
            return ('field', val)
        raise FormulaSyntaxError(f'unexpected {val!r}')


def field_refs(node, out=None):
    out = [] if out is None else out
    if node[0] == 'field':
        if node[1] not in out:
            out.append(node[1])
    elif node[0] == 'bin':
        field_refs(node[2], out)
        field_refs(node[3], out)
    elif node[0] == 'neg':
        field_refs(node[1], out)
    elif node[0] == 'call':
        for a in node[2]:
            field_refs(a, out)
    return out


def parse(text, known_fields=None):
    """Parse a formula. known_fields: names (and ids) that exist; unknown references raise UnknownFieldsError."""
    node = _Parser(text).parse()
    if known_fields is not None:
        unknown = [n for n in field_refs(node) if n not in known_fields]
        if unknown:
            raise UnknownFieldsError(unknown)
    return node


# ---------------------------------------------------------------------------------------------- values

def _num(v):
    if v is BLANK:
        return 0
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s == '':
            return 0
        try:
            return float(s) if any(c in s for c in '.eE') else int(s)
        except ValueError:
            raise FormulaEvalError(f'not a number: {v!r}')
    if isinstance(v, Arr):
        if len(v) == 1:
            return _num(v[0])
        if not v:
            return 0
    raise FormulaEvalError(f'not a number: {v!r}')


def _fmt_num(n):
    if isinstance(n, float):
        if math.isnan(n) or math.isinf(n):
            return 'NaN'
        if n.is_integer():
            return str(int(n))
        return repr(n)
    return str(n)


def to_str(v):
    if v is BLANK or v is None:
        return ''
    if isinstance(v, bool):
        return '1' if v else '0'
    if isinstance(v, (int, float)):
        return _fmt_num(v)
    if isinstance(v, DT):
        return v.iso()
    if isinstance(v, Arr):
        return ', '.join(to_str(x) for x in v)
    return str(v)


def truthy(v):
    if v is BLANK or v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0 and not (isinstance(v, float) and math.isnan(v))
    if isinstance(v, str):
        return v != ''
    if isinstance(v, Arr):
        return len(v) > 0
    if isinstance(v, DT):
        return True
    return bool(v)


def _is_blankish(v):
    return v is BLANK or v is None or v == '' or v is False or (isinstance(v, (int, float)) and not isinstance(v, bool) and v == 0) \
        or (isinstance(v, Arr) and len(v) == 0)


def _dt(v):
    if isinstance(v, DT):
        return v
    if isinstance(v, Arr) and len(v) == 1:
        return _dt(v[0])
    if isinstance(v, str):
        p = parse_datetime(v)
        if p:
            return p
    if v is BLANK:
        return None
    raise FormulaEvalError(f'not a date: {v!r}')


def _numeric(v):
    return isinstance(v, (int, float)) or isinstance(v, bool)


def _numeric_str(v):
    if not isinstance(v, str):
        return False
    try:
        float(v)
        return True
    except ValueError:
        return False


def _equal(a, b):
    if a is BLANK or b is BLANK:
        return _is_blankish(a) and _is_blankish(b)
    if isinstance(a, DT) or isinstance(b, DT):
        da, db = _dt_or_none(a), _dt_or_none(b)
        if da is None or db is None:
            return False
        return da.dt == db.dt
    if _numeric(a) and _numeric(b):
        return _num(a) == _num(b)
    if _numeric(a) and _numeric_str(b) or _numeric(b) and _numeric_str(a):
        return _num(a) == _num(b)
    return to_str(a) == to_str(b)


def _dt_or_none(v):
    try:
        return _dt(v)
    except FormulaEvalError:
        return None


def _order(op, a, b):
    if isinstance(a, DT) or isinstance(b, DT):
        da, db = _dt_or_none(a), _dt_or_none(b)
        if da is None or db is None:
            return False
        x, y = da.dt, db.dt
    elif (_numeric(a) or a is BLANK or _numeric_str(a)) and (_numeric(b) or b is BLANK or _numeric_str(b)):
        x, y = _num(a), _num(b)
    else:
        x, y = to_str(a), to_str(b)
    return {'<': x < y, '>': x > y, '<=': x <= y, '>=': x >= y}[op]


# ---------------------------------------------------------------------------------------------- functions

def _f_if(ctx, args):
    cond = evaluate(args[0], ctx)
    if truthy(cond):
        return evaluate(args[1], ctx)
    return evaluate(args[2], ctx) if len(args) > 2 else BLANK


def _f_switch(ctx, args):
    value = evaluate(args[0], ctx)
    rest = args[1:]
    i = 0
    while i + 1 < len(rest):
        if _equal(value, evaluate(rest[i], ctx)):
            return evaluate(rest[i + 1], ctx)
        i += 2
    return evaluate(rest[-1], ctx) if len(rest) % 2 == 1 else BLANK


def _f_and(ctx, args):
    return all(truthy(evaluate(a, ctx)) for a in args)


def _f_or(ctx, args):
    return any(truthy(evaluate(a, ctx)) for a in args)


def _lazy(fn):
    fn.lazy = True
    return fn


def _flatten(values):
    out = []
    for v in values:
        if isinstance(v, Arr):
            out.extend(_flatten(v))
        else:
            out.append(v)
    return out


def _find(needle, hay, start=0, ci=False):
    n, h = to_str(needle), to_str(hay)
    if ci:
        n, h = n.lower(), h.lower()
    start = int(_num(start)) if start is not BLANK else 0
    idx = h.find(n, max(start - 1, 0))
    return idx + 1 if idx >= 0 else 0


def _round(x, p, mode):
    x, p = _num(x), int(_num(p)) if p is not BLANK else 0
    f = 10 ** p
    if mode == 'up':
        return math.copysign(math.ceil(abs(x) * f) / f, x)
    if mode == 'down':
        return math.copysign(math.floor(abs(x) * f) / f, x)
    return math.copysign(math.floor(abs(x) * f + 0.5) / f, x)


def _diff(a, b, unit):
    da, db = _dt(a), _dt(b)
    if da is None or db is None:
        return BLANK
    unit = _unit(unit)
    if unit in ('months', 'quarters', 'years'):
        months = _month_diff(da.dt, db.dt)
        return int(months / {'months': 1, 'quarters': 3, 'years': 12}[unit])
    secs = (da.dt - db.dt).total_seconds()
    per = {'milliseconds': 0.001, 'seconds': 1, 'minutes': 60, 'hours': 3600, 'days': 86400, 'weeks': 604800}[unit]
    return int(secs / per)


def _dateadd(d, count, unit):
    dv = _dt(d)
    if dv is None:
        return BLANK
    unit, n = _unit(unit), _num(count)
    if unit in ('months', 'quarters', 'years'):
        months = int(n) * {'months': 1, 'quarters': 3, 'years': 12}[unit]
        return DT(_add_months(dv.dt, months), dv.date_only)
    per = {'milliseconds': 0.001, 'seconds': 1, 'minutes': 60, 'hours': 3600, 'days': 86400, 'weeks': 604800}[unit]
    return DT(dv.dt + timedelta(seconds=n * per), dv.date_only and unit in ('days', 'weeks'))


def _is_same(a, b, unit=BLANK):
    da, db = _dt(a), _dt(b)
    if da is None or db is None:
        return False
    if unit is BLANK:
        return da.dt == db.dt
    u = _unit(unit)
    return _truncate(da.dt, u) == _truncate(db.dt, u)


def _parse_with(text, fmt):
    if fmt is BLANK:
        p = parse_datetime(to_str(text))
        if p is None:
            raise FormulaEvalError('cannot parse date')
        return p
    pattern, groups = '', []
    for tok in re.split(r'(YYYY|MM|M|DD|D|HH|H|mm|ss)', to_str(fmt)):
        if tok in ('YYYY',):
            pattern += r'(\d{4})'
            groups.append('Y')
        elif tok in ('MM', 'M'):
            pattern += r'(\d{1,2})'
            groups.append('M')
        elif tok in ('DD', 'D'):
            pattern += r'(\d{1,2})'
            groups.append('D')
        elif tok in ('HH', 'H'):
            pattern += r'(\d{1,2})'
            groups.append('h')
        elif tok == 'mm':
            pattern += r'(\d{2})'
            groups.append('m')
        elif tok == 'ss':
            pattern += r'(\d{2})'
            groups.append('s')
        else:
            pattern += re.escape(tok)
    m = re.fullmatch(pattern, to_str(text).strip())
    if not m:
        raise FormulaEvalError('cannot parse date')
    parts = dict(zip(groups, (int(g) for g in m.groups())))
    has_time = 'h' in parts
    dt = datetime(parts.get('Y', 1970), parts.get('M', 1), parts.get('D', 1), parts.get('h', 0), parts.get('m', 0),
                  parts.get('s', 0), tzinfo=timezone.utc)
    return DT(dt, date_only=not has_time)


def _regex(pattern):
    try:
        return re.compile(to_str(pattern))
    except re.error:
        raise FormulaEvalError('bad regex')


FUNCTIONS = {
    # logical
    'IF': (2, 3, _lazy(_f_if)),
    'SWITCH': (2, None, _lazy(_f_switch)),
    'AND': (1, None, _lazy(_f_and)),
    'OR': (1, None, _lazy(_f_or)),
    'NOT': (1, 1, lambda v: not truthy(v)),
    'XOR': (1, None, lambda *v: sum(1 for x in v if truthy(x)) % 2 == 1),
    'BLANK': (0, 0, lambda: BLANK),
    'TRUE': (0, 0, lambda: True),
    'FALSE': (0, 0, lambda: False),
    'ERROR': (0, 1, lambda *a: (_ for _ in ()).throw(FormulaEvalError('ERROR()'))),
    'ISERROR': (1, 1, lambda v: False),
    # text
    'LOWER': (1, 1, lambda s: to_str(s).lower()),
    'UPPER': (1, 1, lambda s: to_str(s).upper()),
    'TRIM': (1, 1, lambda s: to_str(s).strip()),
    'LEN': (1, 1, lambda s: len(to_str(s))),
    'LEFT': (2, 2, lambda s, n: to_str(s)[:int(_num(n))]),
    'RIGHT': (2, 2, lambda s, n: to_str(s)[-int(_num(n)):] if int(_num(n)) > 0 else ''),
    'MID': (3, 3, lambda s, st, c: to_str(s)[max(int(_num(st)) - 1, 0):max(int(_num(st)) - 1, 0) + int(_num(c))]),
    'FIND': (2, 3, lambda n, h, st=0: _find(n, h, st)),
    'SEARCH': (2, 3, lambda n, h, st=0: _find(n, h, st) or BLANK),
    'SUBSTITUTE': (3, 4, lambda s, old, new, idx=BLANK: to_str(s).replace(to_str(old), to_str(new)) if idx is BLANK else
                   _sub_nth(to_str(s), to_str(old), to_str(new), int(_num(idx)))),
    'REPLACE': (4, 4, lambda s, st, c, new: to_str(s)[:int(_num(st)) - 1] + to_str(new) + to_str(s)[int(_num(st)) - 1 + int(_num(c)):]),
    'CONCATENATE': (1, None, lambda *a: ''.join(to_str(x) for x in a)),
    'REPT': (2, 2, lambda s, n: to_str(s) * int(_num(n))),
    'T': (1, 1, lambda v: v if isinstance(v, str) else ''),
    'VALUE': (1, 1, lambda s: _num(re.sub(r'[^0-9.\-eE]', '', to_str(s)) or '0')),
    'REGEX_MATCH': (2, 2, lambda s, p: bool(_regex(p).search(to_str(s)))),
    'REGEX_EXTRACT': (2, 2, lambda s, p: (lambda m: m.group(0) if m else BLANK)(_regex(p).search(to_str(s)))),
    'REGEX_REPLACE': (3, 3, lambda s, p, r: _regex(p).sub(to_str(r), to_str(s))),
    'ENCODE_URL_COMPONENT': (1, 1, lambda s: urllib.parse.quote(to_str(s), safe='')),
    # numeric
    'ABS': (1, 1, lambda x: abs(_num(x))),
    'ROUND': (1, 2, lambda x, p=0: _round(x, p, 'half')),
    'ROUNDUP': (1, 2, lambda x, p=0: _round(x, p, 'up')),
    'ROUNDDOWN': (1, 2, lambda x, p=0: _round(x, p, 'down')),
    'INT': (1, 1, lambda x: math.floor(_num(x))),
    'CEILING': (1, 2, lambda x, s=1: math.ceil(_num(x) / (_num(s) or 1)) * (_num(s) or 1)),
    'FLOOR': (1, 2, lambda x, s=1: math.floor(_num(x) / (_num(s) or 1)) * (_num(s) or 1)),
    'MOD': (2, 2, lambda a, b: math.fmod(_num(a), _num(b)) if _num(b) else (_ for _ in ()).throw(FormulaEvalError('div0'))),
    'MAX': (1, None, lambda *a: max(_num(x) for x in _flatten(a)) if _flatten(a) else 0),
    'MIN': (1, None, lambda *a: min(_num(x) for x in _flatten(a)) if _flatten(a) else 0),
    'SUM': (1, None, lambda *a: sum(_num(x) for x in _flatten(a))),
    'AVERAGE': (1, None, lambda *a: (sum(_num(x) for x in _flatten(a)) / len(_flatten(a))) if _flatten(a) else 0),
    'COUNT': (1, None, lambda *a: sum(1 for x in _flatten(a) if _numeric(x) and not isinstance(x, bool))),
    'COUNTA': (1, None, lambda *a: sum(1 for x in _flatten(a) if not _is_blankish(x) or x == 0)),
    'COUNTALL': (1, None, lambda *a: len(_flatten(a))),
    # dates
    'TODAY': (0, 0, None),
    'NOW': (0, 0, None),
    'IS_BEFORE': (2, 2, lambda a, b: bool(_dt(a) and _dt(b) and _dt(a).dt < _dt(b).dt)),
    'IS_AFTER': (2, 2, lambda a, b: bool(_dt(a) and _dt(b) and _dt(a).dt > _dt(b).dt)),
    'IS_SAME': (2, 3, _is_same),
    'DATETIME_DIFF': (2, 3, lambda a, b, u=BLANK: _diff(a, b, u)),
    'DATEADD': (3, 3, _dateadd),
    'DATETIME_PARSE': (1, 3, lambda s, fmt=BLANK, locale=BLANK: _parse_with(s, fmt)),
    'DATETIME_FORMAT': (1, 2, lambda d, fmt=BLANK: BLANK if _dt(d) is None else
                        (_dt(d).iso() if fmt is BLANK else _format(_dt(d), to_str(fmt)))),
    'DATESTR': (1, 1, lambda d: BLANK if _dt(d) is None else _dt(d).dt.strftime('%Y-%m-%d')),
    'YEAR': (1, 1, lambda d: _dt(d).dt.year if _dt(d) else BLANK),
    'MONTH': (1, 1, lambda d: _dt(d).dt.month if _dt(d) else BLANK),
    'DAY': (1, 1, lambda d: _dt(d).dt.day if _dt(d) else BLANK),
    'HOUR': (1, 1, lambda d: _dt(d).dt.hour if _dt(d) else BLANK),
    'MINUTE': (1, 1, lambda d: _dt(d).dt.minute if _dt(d) else BLANK),
    'WEEKDAY': (1, 2, _weekday),
    'WEEKNUM': (1, 2, _weeknum),
    # record
    'RECORD_ID': (0, 0, None),
    'CREATED_TIME': (0, 0, None),
    'LAST_MODIFIED_TIME': (0, None, None),
    # arrays
    'ARRAYJOIN': (1, 2, lambda arr, sep=', ': ((', ' if sep is BLANK else to_str(sep)).join(to_str(x) for x in _flatten([arr])))
                  if not (arr is BLANK) else ''),
    'ARRAYUNIQUE': (1, 1, lambda arr: Arr(dict.fromkeys(_flatten([arr]))) if arr is not BLANK else Arr()),
    'ARRAYCOMPACT': (1, 1, lambda arr: Arr(x for x in _flatten([arr]) if not (x is BLANK or x == '' or x is None))),
    'ARRAYFLATTEN': (1, 1, lambda arr: Arr(_flatten([arr]))),
}


def _sub_nth(s, old, new, n):
    idx = -1
    for _ in range(n):
        idx = s.find(old, idx + 1)
        if idx < 0:
            return s
    return s[:idx] + new + s[idx + len(old):]


# ---------------------------------------------------------------------------------------------- evaluation

class Context:
    """What a formula can see. field(name) returns the formula value of a field for the current record."""

    def __init__(self, field, now, record_id='', created_time=None):
        self.field = field
        self.now = now
        self.record_id = record_id
        self.created_time = created_time


def evaluate(node, ctx):
    kind = node[0]
    if kind == 'lit':
        return node[1]
    if kind == 'field':
        return ctx.field(node[1])
    if kind == 'neg':
        return -_num(evaluate(node[1], ctx))
    if kind == 'bin':
        op = node[1]
        a, b = evaluate(node[2], ctx), evaluate(node[3], ctx)
        if op == '=':
            return _equal(a, b)
        if op == '!=':
            return not _equal(a, b)
        if op in ('<', '>', '<=', '>='):
            return _order(op, a, b)
        if op == '&':
            return to_str(a) + to_str(b)
        if op == '+':
            if isinstance(a, DT) or isinstance(b, DT):
                raise FormulaEvalError('use DATEADD to add to dates')
            return _num(a) + _num(b)
        if op == '-':
            return _num(a) - _num(b)
        if op == '*':
            return _num(a) * _num(b)
        if op == '/':
            d = _num(b)
            if d == 0:
                raise FormulaEvalError('division by zero')
            return _num(a) / d
    if kind == 'call':
        name, args = node[1], node[2]
        if name == 'TODAY':
            n = ctx.now
            return DT(datetime(n.year, n.month, n.day, tzinfo=timezone.utc), date_only=True)
        if name == 'NOW':
            return DT(ctx.now)
        if name == 'RECORD_ID':
            return ctx.record_id
        if name in ('CREATED_TIME', 'LAST_MODIFIED_TIME'):
            return DT(ctx.created_time or ctx.now)
        fn = FUNCTIONS[name][2]
        if getattr(fn, 'lazy', False):
            return fn(ctx, args)
        values = [evaluate(a, ctx) for a in args]
        try:
            return fn(*values)
        except (TypeError, ValueError, ZeroDivisionError, OverflowError) as e:
            raise FormulaEvalError(str(e))
    raise FormulaEvalError(f'bad node {kind}')


def matches(node, ctx):
    """True when the record passes the filter. Evaluation errors exclude the record, like #ERROR! in Airtable."""
    try:
        return truthy(evaluate(node, ctx))
    except FormulaEvalError:
        return False
