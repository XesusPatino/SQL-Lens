"""
Compares two extractions (two databases, or the same one on two servers) and
writes an HTML report: what exists only in A, only in B, and what differs.

    python comparar.py extraction_DEV_MESDB.json extraction_PRO_MESDB.json
"""

import argparse
import difflib
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from extraer import COMPARACIONES, NOMBRES_TIPO, buscar_extraccion, en_carpeta, escribir_html
from grafo import TIPO_OBJETO

TIPOS = ['TABLA', 'VISTA', 'PROC', 'FUNCION', 'TRIGGER', 'SINONIMO', 'SECUENCIA', 'JOB']
MAX_LINEAS_DIFF = 1500


def _tipo_columna(c):
    t, largo = c['tipo'], c['max_length']
    if t in ('varchar', 'char', 'varbinary', 'binary'):
        return f"{t}({'max' if largo == -1 else largo})"
    if t in ('nvarchar', 'nchar'):
        return f"{t}({'max' if largo == -1 else largo // 2})"
    if t in ('decimal', 'numeric'):
        return f"{t}({c['precision']},{c['scale']})"
    if t in ('datetime2', 'time', 'datetimeoffset'):
        return f"{t}({c['scale']})"
    return t


def _columnas(filas):
    """Una línea por columna, en su orden: es lo que se compara de una tabla."""
    return '\n'.join(
        f"{c['name']} {_tipo_columna(c)}{'' if c['is_nullable'] else ' NOT NULL'}"
        f"{' IDENTITY' if c['is_identity'] else ''}"
        for c in sorted(filas, key=lambda c: c['column_id']))


def cargar(ruta, base_pedida):
    ex = json.loads(Path(ruta).read_text(encoding='utf-8-sig'))
    bases = list(ex['bases'])
    if base_pedida:
        base = next((b for b in bases if b.lower() == base_pedida.lower()), None)
        if not base:
            sys.exit(f'{ruta} has no database "{base_pedida}" (it has: {", ".join(bases)})')
    elif len(bases) > 1:
        sys.exit(f'{ruta} has several databases ({", ".join(bases)}): choose one with --db-a / --db-b')
    else:
        base = bases[0]
    d = ex['bases'][base]

    modulos = {m['object_id']: m['definition'] for m in d['modulos']}
    sinonimos = {s['object_id']: s['base_object_name'] for s in d.get('sinonimos', ())}
    con_columnas = 'columnas' in d
    columnas = defaultdict(list)
    for c in d.get('columnas', ()):
        columnas[c['object_id']].append(c)

    objetos = {}
    for o in d['objetos']:
        tipo = TIPO_OBJETO.get(o['type'].strip())
        if not tipo:
            continue
        oid = o['object_id']
        if tipo == 'TABLA':
            contenido = _columnas(columnas[oid]) if con_columnas else None
        elif tipo == 'SINONIMO':
            contenido = sinonimos.get(oid)
        elif tipo == 'SECUENCIA':
            contenido = None
        else:
            contenido = modulos.get(oid, '')
        nombre = f"{o['esquema']}.{o['name']}"
        objetos[(tipo, nombre.lower())] = {'n': nombre, 's': contenido, 'm': (o.get('modify_date') or '')[:10]}

    # Los jobs son del servidor, no de la base: se comparan sus pasos.
    pasos = defaultdict(list)
    for p in ex.get('jobs', ()):
        pasos[p['job']].append(p)
    for nombre, lista in pasos.items():
        texto = '\n\n'.join(f"-- Step {p['step_id']}: {p['step_name']} ({p['subsystem']}, database {p['database_name'] or '-'})\n"
                            f"{(p['command'] or '').rstrip()}" for p in sorted(lista, key=lambda p: p['step_id']))
        objetos[('JOB', nombre.lower())] = {'n': nombre, 's': texto, 'm': ''}

    return {'etiqueta': f"{ex['servidor']} · {base}", 'fecha': ex['fecha'],
            'conColumnas': con_columnas, 'conJobs': bool(ex.get('jobs')), 'objetos': objetos}


def _lineas(texto):
    lineas = [l.rstrip() for l in (texto or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    while lineas and not lineas[0]:
        lineas.pop(0)
    while lineas and not lineas[-1]:
        lineas.pop()
    return lineas


def _sin_espacios(lineas):
    return re.sub(r'\s+', ' ', ' '.join(lineas)).strip()


def comparar(a, b):
    resumen = {t: {'a': 0, 'b': 0, 'soloA': 0, 'soloB': 0, 'distintos': 0, 'iguales': 0} for t in TIPOS}
    filas = []
    for clave in sorted(set(a['objetos']) | set(b['objetos']), key=lambda k: (TIPOS.index(k[0]), k[1])):
        tipo = clave[0]
        oa, ob = a['objetos'].get(clave), b['objetos'].get(clave)
        r = resumen[tipo]
        r['a'] += oa is not None
        r['b'] += ob is not None
        fila = {'t': tipo, 'n': (oa or ob)['n'], 'ma': oa and oa['m'], 'mb': ob and ob['m']}
        if ob is None:
            r['soloA'] += 1
            filas.append({**fila, 'e': 'soloA'})
            continue
        if oa is None:
            r['soloB'] += 1
            filas.append({**fila, 'e': 'soloB'})
            continue
        if oa['s'] is None or ob['s'] is None:
            # Sin nada que comparar (secuencias, o tablas extraídas sin columnas): solo cuenta que existe.
            r['iguales'] += 1
            continue
        la, lb = _lineas(oa['s']), _lineas(ob['s'])
        if la == lb or _sin_espacios(la) == _sin_espacios(lb):
            r['iguales'] += 1
            continue
        r['distintos'] += 1
        diff = [l for l in difflib.unified_diff(la, lb, lineterm='', n=3)][2:]
        # Las líneas en blanco no cuentan como cambio.
        mas = sum(1 for l in diff if l.startswith('+') and l[1:].strip())
        menos = sum(1 for l in diff if l.startswith('-') and l[1:].strip())
        if len(diff) > MAX_LINEAS_DIFF:
            diff = diff[:MAX_LINEAS_DIFF] + [f'@@ ... {len(diff) - MAX_LINEAS_DIFF} more lines not shown @@']
        filas.append({**fila, 'e': 'distinto', 'd': '\n'.join(diff), 'mas': mas, 'menos': menos})
    return resumen, filas


def main():
    p = argparse.ArgumentParser(description='Compares two extractions and writes an HTML report.')
    p.add_argument('a', type=Path, metavar='A.json', help='First extraction (e.g. DEV)')
    p.add_argument('b', type=Path, metavar='B.json', help='Second extraction (e.g. PRO)')
    p.add_argument('--db-a', metavar='DATABASE', help='Database to use from A, if it has several')
    p.add_argument('--db-b', metavar='DATABASE', help='Database to use from B, if it has several')
    p.add_argument('--output', type=Path, metavar='FILE.html', help='Output .html file')
    args = p.parse_args()

    a, b = cargar(buscar_extraccion(args.a), args.db_a), cargar(buscar_extraccion(args.b), args.db_b)
    resumen, filas = comparar(a, b)
    avisos = []
    for x in (a, b):
        if not x['conColumnas']:
            avisos.append(f"{x['etiqueta']} ({x['fecha']}) was extracted without table columns: tables are only "
                          'compared by name. Extract it again with the current extraer.ps1 / extraer.py.')
    if a['conJobs'] != b['conJobs']:
        avisos.append('Only one of the two extractions has SQL Agent jobs: the job comparison is not meaningful.')
    datos = {
        'a': {'etiqueta': a['etiqueta'], 'fecha': a['fecha']},
        'b': {'etiqueta': b['etiqueta'], 'fecha': b['fecha']},
        'tipos': TIPOS, 'resumen': resumen, 'filas': filas, 'avisos': avisos,
    }
    salida = en_carpeta(args.output or Path(re.sub(r'[^\w.-]+', '-', f"compare_{a['etiqueta']}_vs_{b['etiqueta']}_{datetime.now():%Y%m%d}") + '.html'),
                        COMPARACIONES)
    escribir_html(datos, salida, plantilla='comparar.html')

    print(f"A: {a['etiqueta']} ({a['fecha']})\nB: {b['etiqueta']} ({b['fecha']})\n")
    print(f"{'':<12}{'A':>7}{'B':>7}{'only A':>9}{'only B':>9}{'differ':>9}")
    for t in TIPOS:
        r = resumen[t]
        if r['a'] or r['b']:
            print(f"{NOMBRES_TIPO[t]:<12}{r['a']:>7}{r['b']:>7}{r['soloA']:>9}{r['soloB']:>9}{r['distintos']:>9}")
    for aviso in avisos:
        print(f'\nWARNING: {aviso}')
    print(f'\nDone: {salida.resolve()}')


if __name__ == '__main__':
    main()
