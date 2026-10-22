"""A small, strict GraphQL engine: SDL schema, query parser, validation, variable coercion and execution.

It exists so the Shopify mock answers like the real Admin API does:
  * only the fields that were selected come back, under their aliases
  * unknown fields, unknown arguments and missing required arguments are rejected before anything runs,
    with graphql-ruby style messages and extensions codes (undefinedField, argumentNotAccepted,
    missingRequiredArguments), HTTP 200 and no data, like Shopify
  * variables are coerced against their declared types; an unknown input field or a wrong scalar type gives
    "Variable $input of type X! was provided invalid value for a.b (...)" with extensions.problems
  * fragments, inline fragments, aliases, __typename and directives on fields are supported
"""
import re

__all__ = ['GraphQLSyntaxError', 'Schema', 'parse_document', 'execute', 'GraphQLFieldError']


class GraphQLSyntaxError(Exception):
    def __init__(self, message, line, col):
        super().__init__(message)
        self.line, self.col = line, col


class GraphQLFieldError(Exception):
    """Raised by a resolver: becomes an entry in "errors" with data for that field set to null."""

    def __init__(self, message, extensions=None):
        super().__init__(message)
        self.extensions = extensions


# ---------------------------------------------------------------------------------------------- lexer

_TOKEN = re.compile(r'''
    (?P<ws>[\s,﻿]+)
  | (?P<comment>\#[^\n]*)
  | (?P<block>"""(?:[^"\\]|\\.|"(?!""))*""")
  | (?P<str>"(?:[^"\\\n]|\\.)*")
  | (?P<spread>\.\.\.)
  | (?P<num>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<name>[_A-Za-z][_0-9A-Za-z]*)
  | (?P<punct>[!$&()\:=@\[\]{|}])
''', re.VERBOSE)

_PUNCT_NAMES = {'{': 'LCURLY', '}': 'RCURLY', '(': 'LPAREN', ')': 'RPAREN', '[': 'LBRACKET', ']': 'RBRACKET', ':': 'COLON',
                '!': 'BANG', '$': 'VAR_SIGN', '=': 'EQUALS', '@': 'DIR_SIGN', '|': 'PIPE', '&': 'AMP'}


def _lex(text):
    toks, pos, line, line_start = [], 0, 1, 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise GraphQLSyntaxError(f'Parse error on "{text[pos]}" at [{line}, {pos - line_start + 1}]', line, pos - line_start + 1)
        kind, val = m.lastgroup, m.group(m.lastgroup)
        col = pos - line_start + 1
        if kind not in ('ws', 'comment'):
            if kind == 'str':
                val = _unescape(val[1:-1])
            elif kind == 'block':
                val = val[3:-3]
                kind = 'str'
            toks.append((kind, val, line, col))
        nl = m.group(0).count('\n')
        if nl:
            line += nl
            line_start = pos + m.group(0).rfind('\n') + 1
        pos = m.end()
    toks.append(('eof', None, line, pos - line_start + 1))
    return toks


def _unescape(s):
    return re.sub(r'\\(u[0-9a-fA-F]{4}|.)', lambda m: chr(int(m.group(1)[1:], 16)) if m.group(1)[0] == 'u' else
                  {'n': '\n', 't': '\t', 'r': '\r', 'b': '\b', 'f': '\f'}.get(m.group(1), m.group(1)), s)


class _P:
    def __init__(self, text):
        self.toks = _lex(text)
        self.i = 0

    def peek(self, k=0):
        return self.toks[self.i + k]

    def at(self, kind, val=None):
        t = self.peek()
        return t[0] == kind and (val is None or t[1] == val)

    def take(self, kind=None, val=None):
        t = self.peek()
        if (kind and t[0] != kind) or (val is not None and t[1] != val):
            label = 'end of file' if t[0] == 'eof' else f'"{t[1]}"'
            tname = 'EOF' if t[0] == 'eof' else _PUNCT_NAMES.get(t[1], 'IDENTIFIER' if t[0] == 'name' else t[0].upper())
            raise GraphQLSyntaxError(f'Parse error on {label} ({tname}) at [{t[2]}, {t[3]}]', t[2], t[3])
        self.i += 1
        return t


# ---------------------------------------------------------------------------------------------- type refs

class TypeRef:
    __slots__ = ('name', 'non_null', 'of')

    def __init__(self, name=None, non_null=False, of=None):
        self.name, self.non_null, self.of = name, non_null, of

    @property
    def is_list(self):
        return self.of is not None

    def named(self):
        t = self
        while t.of is not None:
            t = t.of
        return t.name

    def __str__(self):
        inner = f'[{self.of}]' if self.of is not None else self.name
        return inner + ('!' if self.non_null else '')


def _parse_type(p):
    if p.at('punct', '['):
        p.take()
        inner = _parse_type(p)
        p.take('punct', ']')
        t = TypeRef(of=inner)
    else:
        t = TypeRef(name=p.take('name')[1])
    if p.at('punct', '!'):
        p.take()
        t.non_null = True
    return t


def parse_type_string(s):
    return _parse_type(_P(s))


# ---------------------------------------------------------------------------------------------- values (AST)

def _parse_value(p, const=False):
    t = p.peek()
    if t[0] == 'punct' and t[1] == '$' and not const:
        p.take()
        name = p.take('name')
        return ('var', name[1], name[2], name[3])
    if t[0] == 'num':
        p.take()
        return ('float', float(t[1])) if any(c in t[1] for c in '.eE') else ('int', int(t[1]))
    if t[0] == 'str':
        p.take()
        return ('str', t[1])
    if t[0] == 'name':
        p.take()
        if t[1] in ('true', 'false'):
            return ('bool', t[1] == 'true')
        if t[1] == 'null':
            return ('null', None)
        return ('enum', t[1])
    if t[0] == 'punct' and t[1] == '[':
        p.take()
        items = []
        while not p.at('punct', ']'):
            items.append(_parse_value(p, const))
        p.take()
        return ('list', items)
    if t[0] == 'punct' and t[1] == '{':
        p.take()
        fields = []
        while not p.at('punct', '}'):
            k = p.take('name')[1]
            p.take('punct', ':')
            fields.append((k, _parse_value(p, const)))
        p.take()
        return ('obj', fields)
    p.take('name')  # raises a parse error


def _vars_in(value, out):
    if value[0] == 'var':
        out.append(value)
    elif value[0] == 'list':
        for v in value[1]:
            _vars_in(v, out)
    elif value[0] == 'obj':
        for _, v in value[1]:
            _vars_in(v, out)
    return out


# ---------------------------------------------------------------------------------------------- executable documents

class Field:
    __slots__ = ('alias', 'name', 'args', 'directives', 'selections', 'line', 'col')

    def __init__(self, alias, name, args, directives, selections, line, col):
        self.alias, self.name, self.args, self.directives = alias, name, args, directives
        self.selections, self.line, self.col = selections, line, col

    @property
    def key(self):
        return self.alias or self.name


class Spread:
    __slots__ = ('name', 'line', 'col')

    def __init__(self, name, line, col):
        self.name, self.line, self.col = name, line, col


class Inline:
    __slots__ = ('type_condition', 'selections', 'line', 'col')

    def __init__(self, type_condition, selections, line, col):
        self.type_condition, self.selections, self.line, self.col = type_condition, selections, line, col


class Operation:
    def __init__(self, kind, name, var_defs, directives, selections, line, col):
        self.kind, self.name, self.var_defs, self.directives = kind, name, var_defs, directives
        self.selections, self.line, self.col = selections, line, col


class Document:
    def __init__(self, operations, fragments):
        self.operations, self.fragments = operations, fragments


def _parse_args(p):
    args = []
    if p.at('punct', '('):
        p.take()
        while not p.at('punct', ')'):
            t = p.take('name')
            p.take('punct', ':')
            args.append((t[1], _parse_value(p), t[2], t[3]))
        p.take()
    return args


def _parse_directives(p):
    out = []
    while p.at('punct', '@'):
        p.take()
        t = p.take('name')
        out.append((t[1], _parse_args(p), t[2], t[3]))
    return out


def _parse_selections(p):
    p.take('punct', '{')
    sels = []
    while not p.at('punct', '}'):
        if p.at('spread'):
            t = p.take()
            if p.at('name') and p.peek()[1] != 'on':
                n = p.take('name')
                _parse_directives(p)
                sels.append(Spread(n[1], t[2], t[3]))
            else:
                cond = None
                if p.at('name', 'on'):
                    p.take()
                    cond = p.take('name')[1]
                _parse_directives(p)
                sels.append(Inline(cond, _parse_selections(p), t[2], t[3]))
            continue
        t = p.take('name')
        alias, name = None, t[1]
        if p.at('punct', ':'):
            p.take()
            alias, name = name, p.take('name')[1]
        args = _parse_args(p)
        dirs = _parse_directives(p)
        subs = _parse_selections(p) if p.at('punct', '{') else None
        sels.append(Field(alias, name, args, dirs, subs, t[2], t[3]))
    p.take()
    if not sels:
        t = p.peek()
        raise GraphQLSyntaxError(f'Parse error on "}}" (RCURLY) at [{t[2]}, {t[3]}]', t[2], t[3])
    return sels


def parse_document(text):
    p = _P(text)
    ops, frags = [], {}
    while not p.at('eof'):
        t = p.peek()
        if t[0] == 'punct' and t[1] == '{':
            ops.append(Operation('query', None, [], [], _parse_selections(p), t[2], t[3]))
            continue
        if t[0] == 'name' and t[1] in ('query', 'mutation', 'subscription'):
            p.take()
            name = p.take('name')[1] if p.at('name') else None
            var_defs = []
            if p.at('punct', '('):
                p.take()
                while not p.at('punct', ')'):
                    p.take('punct', '$')
                    vt = p.take('name')
                    p.take('punct', ':')
                    vtype = _parse_type(p)
                    default = None
                    if p.at('punct', '='):
                        p.take()
                        default = _parse_value(p, const=True)
                    var_defs.append((vt[1], vtype, default, vt[2], vt[3]))
                p.take()
            dirs = _parse_directives(p)
            ops.append(Operation(t[1], name, var_defs, dirs, _parse_selections(p), t[2], t[3]))
            continue
        if t[0] == 'name' and t[1] == 'fragment':
            p.take()
            name = p.take('name')[1]
            p.take('name', 'on')
            cond = p.take('name')[1]
            _parse_directives(p)
            frags[name] = (cond, _parse_selections(p))
            continue
        p.take('punct', '{')
    if not ops:
        raise GraphQLSyntaxError('Parse error on end of file (EOF) at [1, 1]', 1, 1)
    return Document(ops, frags)


# ---------------------------------------------------------------------------------------------- schema (SDL subset)

class FieldDef:
    __slots__ = ('name', 'type', 'args', 'cost')

    def __init__(self, name, type_, args):
        self.name, self.type, self.args, self.cost = name, type_, args, None


class TypeDef:
    def __init__(self, kind, name):
        self.kind, self.name = kind, name
        self.fields = {}       # object and input types
        self.values = []       # enums
        self.members = []      # unions
        self.interfaces = []


SCALARS = {'ID', 'String', 'Int', 'Float', 'Boolean'}


class Schema:
    def __init__(self, sdl, query='QueryRoot', mutation='Mutation', directives=('idempotent', 'include', 'skip', 'deprecated')):
        self.types = {n: TypeDef('scalar', n) for n in SCALARS}
        self.query, self.mutation = query, mutation
        self.directives = set(directives)
        self._parse_sdl(sdl)

    def _parse_sdl(self, sdl):
        p = _P(sdl)
        while not p.at('eof'):
            kw = p.take('name')[1]
            if kw == 'scalar':
                n = p.take('name')[1]
                self.types[n] = TypeDef('scalar', n)
            elif kw == 'enum':
                n = p.take('name')[1]
                td = TypeDef('enum', n)
                p.take('punct', '{')
                while not p.at('punct', '}'):
                    td.values.append(p.take('name')[1])
                p.take()
                self.types[n] = td
            elif kw == 'union':
                n = p.take('name')[1]
                td = TypeDef('union', n)
                p.take('punct', '=')
                td.members.append(p.take('name')[1])
                while p.at('punct', '|'):
                    p.take()
                    td.members.append(p.take('name')[1])
                self.types[n] = td
            elif kw in ('type', 'input', 'interface'):
                n = p.take('name')[1]
                td = TypeDef({'type': 'object', 'input': 'input', 'interface': 'interface'}[kw], n)
                if p.at('name', 'implements'):
                    p.take()
                    td.interfaces.append(p.take('name')[1])
                    while p.at('punct', '&'):
                        p.take()
                        td.interfaces.append(p.take('name')[1])
                p.take('punct', '{')
                while not p.at('punct', '}'):
                    fname = p.take('name')[1]
                    args = {}
                    if p.at('punct', '('):
                        p.take()
                        while not p.at('punct', ')'):
                            an = p.take('name')[1]
                            p.take('punct', ':')
                            at = _parse_type(p)
                            default = None
                            if p.at('punct', '='):
                                p.take()
                                default = _parse_value(p, const=True)
                            args[an] = (at, default)
                        p.take()
                    p.take('punct', ':')
                    ftype = _parse_type(p)
                    if p.at('punct', '='):
                        p.take()
                        _parse_value(p, const=True)
                    _parse_directives(p)
                    td.fields[fname] = FieldDef(fname, ftype, args)
                p.take()
                self.types[n] = td
            else:
                raise GraphQLSyntaxError(f'unknown SDL keyword {kw}', 0, 0)

    def possible_types(self, name):
        td = self.types[name]
        if td.kind == 'union':
            return set(td.members)
        if td.kind == 'interface':
            return {n for n, t in self.types.items() if name in t.interfaces}
        return {name}


# ---------------------------------------------------------------------------------------------- errors

def _loc(line, col):
    return [{'line': line, 'column': col}]


class _Errors(list):
    def add(self, message, line=None, col=None, path=None, extensions=None):
        e = {'message': message}
        if line is not None:
            e['locations'] = _loc(line, col)
        if path is not None:
            e['path'] = path
        if extensions is not None:
            e['extensions'] = extensions
        self.append(e)


# ---------------------------------------------------------------------------------------------- validation

def _op_label(op):
    return op.name or f'anonymous {op.kind}'


def validate(schema, doc, op):
    errors = _Errors()
    root = schema.query if op.kind == 'query' else schema.mutation if op.kind == 'mutation' else None
    if root is None or root not in schema.types:
        errors.add(f"Schema is not configured for {op.kind}s", op.line, op.col)
        return errors
    declared = {v[0]: v for v in op.var_defs}
    used = []
    for name, vtype, default, line, col in op.var_defs:
        if vtype.named() not in schema.types:
            errors.add(f'{vtype.named()} isn\'t a defined input type (on ${name})', line, col,
                       extensions={'code': 'variableRequiresValidType', 'typeName': vtype.named(), 'variableName': name})
        elif schema.types[vtype.named()].kind not in ('scalar', 'enum', 'input'):
            errors.add(f'{vtype.named()} isn\'t a valid input type (on ${name})', line, col,
                       extensions={'code': 'variableRequiresValidType', 'typeName': vtype.named(), 'variableName': name})
    for name, args, line, col in op.directives:
        if name not in schema.directives:
            errors.add(f"Directive @{name} is not defined", line, col,
                       extensions={'code': 'undefinedDirective', 'directiveName': name})
    visited = set()
    spread_names = set()

    def walk(selections, type_name, path):
        td = schema.types.get(type_name)
        for sel in selections:
            if isinstance(sel, Spread):
                spread_names.add(sel.name)
                frag = doc.fragments.get(sel.name)
                if frag is None:
                    errors.add(f"Fragment {sel.name} was used, but not defined", sel.line, sel.col, path,
                               {'code': 'useAndDefineFragment', 'fragmentName': sel.name})
                    continue
                if frag[0] not in schema.types:
                    errors.add(f"No such type {frag[0]}, so it can't be a fragment condition", sel.line, sel.col, path,
                               {'code': 'undefinedType', 'typeName': frag[0]})
                    continue
                if td and not (schema.possible_types(frag[0]) & schema.possible_types(type_name)):
                    errors.add(f"Fragment {sel.name} on {frag[0]} can't be spread inside {type_name}", sel.line, sel.col,
                               path, {'code': 'cannotSpreadFragment', 'typeName': type_name, 'fragmentName': sel.name})
                    continue
                if sel.name in visited:
                    continue
                visited.add(sel.name)
                walk(frag[1], frag[0], path + [f'... {sel.name}'])
                continue
            if isinstance(sel, Inline):
                cond = sel.type_condition or type_name
                if cond not in schema.types:
                    errors.add(f"No such type {cond}, so it can't be a fragment condition", sel.line, sel.col, path,
                               {'code': 'undefinedType', 'typeName': cond})
                    continue
                if td and not (schema.possible_types(cond) & schema.possible_types(type_name)):
                    errors.add(f"Fragment on {cond} can't be spread inside {type_name}", sel.line, sel.col, path,
                               {'code': 'cannotSpreadFragment', 'typeName': type_name, 'fragmentName': 'unknown'})
                    continue
                walk(sel.selections, cond, path + [f'... on {cond}'])
                continue
            fpath = path + [sel.key]
            if sel.name == '__typename':
                continue
            fd = td.fields.get(sel.name) if td else None
            if fd is None:
                errors.add(f"Field '{sel.name}' doesn't exist on type '{type_name}'", sel.line, sel.col, fpath,
                           {'code': 'undefinedField', 'typeName': type_name, 'fieldName': sel.name})
                continue
            given = {a[0] for a in sel.args}
            for aname, aval, line, col in sel.args:
                if aname not in fd.args:
                    errors.add(f"Field '{sel.name}' doesn't accept argument '{aname}'", line, col, fpath + [aname],
                               {'code': 'argumentNotAccepted', 'name': sel.name, 'typeName': 'Field', 'argumentName': aname})
                else:
                    used.extend((v, sel.name) for v in _vars_in(aval, []))
                    if aval[0] != 'var':
                        problems = coerce_literal(schema, fd.args[aname][0], aval, declared)
                        for prob in problems:
                            errors.add(f"Argument '{aname}' on Field '{sel.name}' has an invalid value ({_literal_text(aval)}). "
                                       f"Expected type '{fd.args[aname][0]}'.", line, col, fpath + [aname],
                                       {'code': 'argumentLiteralsIncompatible', 'typeName': 'Field', 'argumentName': aname})
                            break
            missing = [a for a, (t, default) in fd.args.items() if t.non_null and default is None and a not in given]
            if missing:
                errors.add(f"Field '{sel.name}' is missing required arguments: {', '.join(missing)}", sel.line, sel.col, fpath,
                           {'code': 'missingRequiredArguments', 'className': 'Field', 'name': sel.name,
                            'arguments': ', '.join(missing)})
            for dname, dargs, line, col in sel.directives:
                if dname not in schema.directives:
                    errors.add(f"Directive @{dname} is not defined", line, col, fpath,
                               {'code': 'undefinedDirective', 'directiveName': dname})
                for _, dval, _, _ in dargs:
                    used.extend((v, sel.name) for v in _vars_in(dval, []))
            inner = fd.type.named()
            kind = schema.types[inner].kind
            if kind in ('scalar', 'enum'):
                if sel.selections:
                    errors.add(f"Selections can't be made on {'scalars' if kind == 'scalar' else 'enums'} "
                               f"(field '{sel.name}' returns {inner} but has selections [{', '.join(_sel_names(sel.selections))}])",
                               sel.line, sel.col, fpath, {'code': 'selectionMismatch', 'nodeName': f"field '{sel.name}'",
                                                          'typeName': inner})
            elif not sel.selections:
                errors.add(f"Field must have selections (field '{sel.name}' returns {inner} but has no selections. "
                           f"Did you mean '{sel.name} {{ ... }}'?)", sel.line, sel.col, fpath,
                           {'code': 'selectionMismatch', 'nodeName': f"field '{sel.name}'", 'typeName': inner})
            else:
                walk(sel.selections, inner, fpath)

    walk(op.selections, root, [op.kind if not op.name else f'{op.kind} {op.name}'])
    if len(doc.operations) == 1:
        for fname in doc.fragments:
            if fname not in spread_names:
                errors.add(f'Fragment {fname} was defined, but not used', extensions={'code': 'useAndDefineFragment',
                                                                                      'fragmentName': fname})
    for var, field_name in used:
        if var[1] not in declared:
            errors.add(f"Variable ${var[1]} is used by {_op_label(op)} but not declared", var[2], var[3],
                       extensions={'code': 'variableNotDefined', 'variableName': var[1]})
    used_names = {v[0][1] for v in used}
    for name, vtype, default, line, col in op.var_defs:
        if name not in used_names:
            errors.add(f"Variable ${name} is declared by {_op_label(op)} but not used", line, col,
                       extensions={'code': 'variableNotUsed', 'variableName': name})
    return errors


def _sel_names(sels):
    return [s.key if isinstance(s, Field) else '...' for s in sels]


def _literal_text(v):
    kind = v[0]
    if kind == 'str':
        return '"' + v[1] + '"'
    if kind in ('int', 'float'):
        return str(v[1])
    if kind == 'bool':
        return 'true' if v[1] else 'false'
    if kind == 'null':
        return 'null'
    if kind == 'enum':
        return v[1]
    if kind == 'list':
        return '[' + ', '.join(_literal_text(x) for x in v[1]) + ']'
    if kind == 'obj':
        return '{' + ', '.join(f'{k}: {_literal_text(x)}' for k, x in v[1]) + '}'
    return '$' + v[1]


# ---------------------------------------------------------------------------------------------- coercion

def coerce_value(schema, tref, value, path, problems):
    """Coerce a JSON variable value to tref. Appends {'path', 'explanation'} problems."""
    if value is None:
        if tref.non_null:
            problems.append({'path': path, 'explanation': 'Expected value to not be null'})
        return None
    if tref.is_list:
        items = value if isinstance(value, list) else [value]
        return [coerce_value(schema, tref.of, v, path + [i], problems) for i, v in enumerate(items)]
    td = schema.types.get(tref.name)
    if td is None:
        problems.append({'path': path, 'explanation': f'Unknown type {tref.name}'})
        return None
    if td.kind == 'input':
        if not isinstance(value, dict):
            problems.append({'path': path, 'explanation': f'Expected "{_short(value)}" to be a key-value object.'})
            return None
        out = {}
        for k, v in value.items():
            if k not in td.fields:
                problems.append({'path': path + [k], 'explanation': f'Field is not defined on {td.name}'})
                continue
            out[k] = coerce_value(schema, td.fields[k].type, v, path + [k], problems)
        for k, fd in td.fields.items():
            if fd.type.non_null and k not in value:
                problems.append({'path': path + [k], 'explanation': 'Expected value to not be null'})
        return out
    if td.kind == 'enum':
        if not isinstance(value, str) or value not in td.values:
            problems.append({'path': path, 'explanation': f'Expected "{_short(value)}" to be one of: {", ".join(td.values)}'})
            return None
        return value
    return _coerce_scalar(td.name, value, path, problems)


def _short(v):
    s = v if isinstance(v, str) else repr(v)
    return s if len(s) < 60 else s[:57] + '...'


def _coerce_scalar(name, value, path, problems):
    def bad():
        problems.append({'path': path, 'explanation': f'Could not coerce value {_json(value)} to {name}'})
        return None
    if name == 'Int':
        if isinstance(value, bool) or not isinstance(value, int) or not -2 ** 31 <= value < 2 ** 31:
            if isinstance(value, float) and value.is_integer():
                return int(value)
            return bad()
        return value
    if name == 'Float':
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return bad()
        return float(value)
    if name == 'Boolean':
        if not isinstance(value, bool):
            return bad()
        return value
    if name == 'ID':
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return bad()
        return str(value)
    if name in ('Decimal', 'Money'):
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return bad()
        try:
            float(value)
        except ValueError:
            return bad()
        return str(value)
    if name == 'UnsignedInt64':
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return bad()
        return str(value)
    if name == 'DateTime':
        if not isinstance(value, str) or not re.match(r'^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?)?$', value):
            return bad()
        return value
    if name == 'JSON':
        return value
    # String, URL, HTML, FormattedString and other text scalars
    if not isinstance(value, str):
        return bad()
    return value


def _json(v):
    import json
    return json.dumps(v)


def literal_to_python(value, variables):
    kind = value[0]
    if kind == 'var':
        return variables.get(value[1])
    if kind in ('int', 'float', 'str', 'bool', 'enum'):
        return value[1]
    if kind == 'null':
        return None
    if kind == 'list':
        return [literal_to_python(v, variables) for v in value[1]]
    if kind == 'obj':
        return {k: literal_to_python(v, variables) for k, v in value[1]}


def coerce_literal(schema, tref, value, declared):
    """Problems with an inline literal argument (variables inside are not checked here)."""
    problems = []
    if value[0] == 'var':
        return problems
    if value[0] == 'null':
        if tref.non_null:
            problems.append('null')
        return problems
    if tref.is_list:
        items = value[1] if value[0] == 'list' else [value]
        for v in items:
            problems += coerce_literal(schema, tref.of, v, declared)
        return problems
    td = schema.types.get(tref.name)
    if td.kind == 'input':
        if value[0] != 'obj':
            return ['object']
        given = {k for k, _ in value[1]}
        for k, v in value[1]:
            if k not in td.fields:
                problems.append(k)
            else:
                problems += coerce_literal(schema, td.fields[k].type, v, declared)
        for k, fd in td.fields.items():
            if fd.type.non_null and k not in given:
                problems.append(k)
        return problems
    if td.kind == 'enum':
        if value[0] != 'enum' or value[1] not in td.values:
            problems.append('enum')
        return problems
    py = literal_to_python(value, {})
    if value[0] == 'enum':
        return ['enum literal']
    sub = []
    _coerce_scalar(td.name, py, [], sub)
    if td.name == 'Float' and value[0] == 'int':
        sub = []
    return problems + sub


# ---------------------------------------------------------------------------------------------- execution

class Info:
    """Passed to resolvers."""

    def __init__(self, schema, field, field_def, parent_type, path, variables, context, directives):
        self.schema, self.field, self.field_def, self.parent_type = schema, field, field_def, parent_type
        self.path, self.variables, self.context, self.directives = path, variables, context, directives


def execute(schema, doc, resolvers, variables=None, operation_name=None, context=None, type_resolver=None):
    """Run the document. Returns (result_dict, operation). resolvers: {'Type.field': fn(parent, args, info)}.
    Fields without a resolver read parent[field] from dict parents."""
    ops = doc.operations
    if operation_name:
        ops = [o for o in ops if o.name == operation_name]
        if not ops:
            return {'errors': [{'message': f'No operation named "{operation_name}"'}]}, None
    elif len(ops) > 1:
        return {'errors': [{'message': 'An operation name is required'}]}, None
    op = ops[0]
    errors = validate(schema, doc, op)
    if errors:
        return {'errors': list(errors)}, op
    raw_vars = variables or {}
    if not isinstance(raw_vars, dict):
        return {'errors': [{'message': 'Variables must be a JSON object'}]}, op
    values = {}
    var_errors = _Errors()
    for name, vtype, default, line, col in op.var_defs:
        if name not in raw_vars:
            if default is not None:
                values[name] = literal_to_python(default, {})
                continue
            if vtype.non_null:
                var_errors.add(f'Variable ${name} of type {vtype} was provided invalid value', line, col,
                               extensions={'value': None, 'problems': [{'path': [], 'explanation': 'Expected value to not be null'}]})
            continue
        problems = []
        values[name] = coerce_value(schema, vtype, raw_vars[name], [], problems)
        if problems:
            where = ', '.join('.'.join(str(x) for x in pr['path']) for pr in problems if pr['path'])
            explain = '; '.join(pr['explanation'] for pr in problems)
            msg = f'Variable ${name} of type {vtype} was provided invalid value'
            if where:
                msg += f' for {where}'
            msg += f' ({explain})'
            var_errors.add(msg, line, col, extensions={'value': raw_vars[name], 'problems': problems})
    if var_errors:
        return {'errors': list(var_errors)}, op
    run_errors = _Errors()
    root_type = schema.query if op.kind == 'query' else schema.mutation
    try:
        data = _exec_selections(schema, doc, op.selections, root_type, {}, [], values, resolvers, context, run_errors,
                                type_resolver, serial=op.kind == 'mutation')
    except _NullBubble:
        data = None
    out = {'data': data}
    if run_errors:
        out['errors'] = list(run_errors)
    return out, op


def _collect(schema, doc, selections, type_name, variables, type_resolver, parent):
    fields = []
    for sel in selections:
        if isinstance(sel, Field):
            if _skipped(sel.directives, variables):
                continue
            fields.append(sel)
        elif isinstance(sel, Spread):
            cond, subs = doc.fragments[sel.name]
            if type_name in schema.possible_types(cond):
                fields += _collect(schema, doc, subs, type_name, variables, type_resolver, parent)
        else:
            cond = sel.type_condition
            if cond is None or type_name in schema.possible_types(cond):
                fields += _collect(schema, doc, sel.selections, type_name, variables, type_resolver, parent)
    return fields


def _skipped(directives, variables):
    for name, args, _, _ in directives:
        vals = {a[0]: literal_to_python(a[1], variables) for a in args}
        if name == 'skip' and vals.get('if'):
            return True
        if name == 'include' and not vals.get('if', True):
            return True
    return False


def _exec_selections(schema, doc, selections, type_name, parent, path, variables, resolvers, context, errors,
                     type_resolver, serial=False):
    out = {}
    for f in _collect(schema, doc, selections, type_name, variables, type_resolver, parent):
        if f.key in out and f.name != '__typename':
            # merge sub selections of the same response key
            continue
        if f.name == '__typename':
            out[f.key] = type_name
            continue
        fd = schema.types[type_name].fields[f.name]
        args = {}
        for aname, (atype, default) in fd.args.items():
            given = next((a for a in f.args if a[0] == aname), None)
            if given is not None:
                if given[1][0] == 'var' and given[1][1] not in variables:
                    if default is not None:
                        args[aname] = literal_to_python(default, {})
                    continue
                args[aname] = literal_to_python(given[1], variables)
            elif default is not None:
                args[aname] = literal_to_python(default, {})
        directives = {d[0]: {a[0]: literal_to_python(a[1], variables) for a in d[1]} for d in f.directives}
        fpath = path + [f.key]
        info = Info(schema, f, fd, type_name, fpath, variables, context, directives)
        resolver = resolvers.get(f'{type_name}.{f.name}')
        try:
            if resolver:
                value = resolver(parent, args, info)
            elif isinstance(parent, dict):
                value = parent.get(f.name)
            else:
                value = getattr(parent, f.name, None)
            out[f.key] = _complete(schema, doc, fd.type, f, value, fpath, variables, resolvers, context, errors, type_resolver)
        except GraphQLFieldError as e:
            # the error is reported once, at the field that failed; a non-null field passes the null up to its parent
            errors.add(str(e), f.line, f.col, fpath, e.extensions)
            if fd.type.non_null:
                raise _NullBubble()
            out[f.key] = None
        except _NullBubble:
            if fd.type.non_null:
                raise
            out[f.key] = None
    return out


class _NullBubble(Exception):
    """A non-null field resolved to null: its parent becomes null too (GraphQL spec, 'Handling field errors')."""


def _complete(schema, doc, tref, f, value, path, variables, resolvers, context, errors, type_resolver):
    if value is None:
        if tref.non_null:
            raise GraphQLFieldError(f'Cannot return null for non-nullable field {".".join(str(p) for p in path)}')
        return None
    if tref.is_list:
        return [_complete(schema, doc, tref.of, f, v, path + [i], variables, resolvers, context, errors, type_resolver)
                for i, v in enumerate(value)]
    td = schema.types[tref.name]
    if td.kind in ('scalar', 'enum'):
        return value
    concrete = tref.name
    if td.kind in ('union', 'interface'):
        concrete = type_resolver(value) if type_resolver else value.get('__typename')
    return _exec_selections(schema, doc, f.selections, concrete, value, path, variables, resolvers, context, errors,
                            type_resolver)


# ---------------------------------------------------------------------------------------------- cost

def requested_cost(schema, doc, op, variables, connection_suffix='Connection'):
    """Shopify style cost: objects 1, connections 2 + first * child cost, scalars 0, each mutation field 10."""
    root = schema.query if op.kind == 'query' else schema.mutation

    def cost(selections, type_name, depth=0):
        total = 0
        for sel in selections:
            if isinstance(sel, Spread):
                cond, subs = doc.fragments[sel.name]
                total += cost(subs, cond, depth)
                continue
            if isinstance(sel, Inline):
                total += cost(sel.selections, sel.type_condition or type_name, depth)
                continue
            if sel.name == '__typename':
                continue
            fd = schema.types[type_name].fields.get(sel.name)
            if fd is None:
                continue
            inner = fd.type.named()
            kind = schema.types[inner].kind
            if kind in ('scalar', 'enum'):
                continue
            child = cost(sel.selections or [], inner, depth + 1)
            if inner.endswith(connection_suffix):
                args = {a[0]: literal_to_python(a[1], variables) for a in sel.args}
                n = args.get('first') or args.get('last') or 0
                total += 2 + int(n or 0) * max(child - 1, 1) if child else 2
            elif depth == 0 and op.kind == 'mutation':
                total += 10
            else:
                total += 1 + child
        return total
    return cost(op.selections, root)
