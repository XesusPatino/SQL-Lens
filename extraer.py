"""
Extracts the dependencies of one or more SQL Server databases and builds the map.
"""

import argparse
import getpass
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from grafo import construir

CONSULTAS = {
    'objetos': """
        SELECT o.object_id, s.name AS esquema, o.name, o.type, o.create_date,
               o.modify_date, o.parent_object_id
        FROM {bd}.sys.objects o
        JOIN {bd}.sys.schemas s ON s.schema_id = o.schema_id
        WHERE o.is_ms_shipped = 0""",
    'modulos': """
        SELECT m.object_id, m.definition
        FROM {bd}.sys.sql_modules m
        JOIN {bd}.sys.objects o ON o.object_id = m.object_id
        WHERE o.is_ms_shipped = 0""",
    'filas': """
        SELECT p.object_id, SUM(p.rows) AS filas
        FROM {bd}.sys.partitions p
        WHERE p.index_id IN (0, 1)
        GROUP BY p.object_id""",
    'dependencias': """
        SELECT DISTINCT d.referencing_id, d.referenced_id, d.referenced_server_name,
               d.referenced_database_name, d.referenced_schema_name,
               d.referenced_entity_name
        FROM {bd}.sys.sql_expression_dependencies d
        WHERE d.referencing_class = 1 AND d.referenced_class = 1""",
    'sinonimos': """
        SELECT s.object_id, s.base_object_name
        FROM {bd}.sys.synonyms s""",
    # Table columns: only used by comparar.py, to tell whether a table changed.
    'columnas': """
        SELECT c.object_id, c.column_id, c.name, t.name AS tipo, c.max_length,
               c.precision, c.scale, c.is_nullable, c.is_identity
        FROM {bd}.sys.columns c
        JOIN {bd}.sys.objects o ON o.object_id = c.object_id
        JOIN {bd}.sys.types t ON t.user_type_id = c.user_type_id
        WHERE o.type = 'U' AND o.is_ms_shipped = 0""",
}
# These can fail (permissions) without stopping the extraction.
OPCIONALES = ('filas', 'sinonimos', 'columnas')

SISTEMA = "SELECT DISTINCT name FROM sys.system_objects"

JOBS = """
    SELECT j.job_id, j.name AS job, j.enabled, s.step_id, s.step_name, s.subsystem,
           s.database_name, s.command
    FROM msdb.dbo.sysjobs j
    JOIN msdb.dbo.sysjobsteps s ON s.job_id = j.job_id
    ORDER BY j.name, s.step_id"""

DRIVERS_PREFERIDOS = [
    'ODBC Driver 18 for SQL Server',
    'ODBC Driver 17 for SQL Server',
    'ODBC Driver 13 for SQL Server',
    'SQL Server Native Client 11.0',
    'SQL Server',
]


def elegir_driver(pyodbc, pedido):
    if pedido:
        return pedido
    instalados = set(pyodbc.drivers())
    for d in DRIVERS_PREFERIDOS:
        if d in instalados:
            return d
    sys.exit('No SQL Server ODBC driver is installed. Install Microsoft\'s '
             '"ODBC Driver 18 for SQL Server", or choose one with --driver.\n'
             f'Installed: {", ".join(sorted(instalados)) or "none"}')


def conectar(args):
    try:
        import pyodbc
    except ImportError:
        sys.exit('pyodbc is missing:  pip install pyodbc')
    driver = elegir_driver(pyodbc, args.driver)
    partes = [f'DRIVER={{{driver}}}', f'SERVER={args.servidor}', 'DATABASE=master',
              'APP=SQL Map (read only)', 'ApplicationIntent=ReadOnly']
    if args.usuario:
        contrasena = args.contrasena or getpass.getpass(f'Password for {args.usuario}: ')
        partes += [f'UID={args.usuario}', f'PWD={{{contrasena.replace("}", "}}")}}}']
    else:
        partes.append('Trusted_Connection=yes')
    if 'ODBC Driver' in driver:
        # El 18 cifra por defecto y rechaza certificados autofirmados.
        partes += ['Encrypt=yes', 'TrustServerCertificate=yes']
    print(f'Connecting to {args.servidor} with "{driver}"...')
    try:
        conexion = pyodbc.connect(';'.join(partes), autocommit=True, timeout=30)
    except pyodbc.Error as e:
        sys.exit(f'Could not connect: {e}')
    conexion.timeout = args.timeout
    return conexion


def leer(cursor, sql, *parametros):
    cursor.execute(sql, *parametros)
    columnas = [c[0] for c in cursor.description]
    return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]


def entre_corchetes(nombre):
    return '[' + nombre.replace(']', ']]') + ']'


def extraer(args):
    conexion = conectar(args)
    cursor = conexion.cursor()
    servidor = leer(cursor, "SELECT @@SERVERNAME AS s")[0]['s'] or args.servidor
    extraccion = {
        'servidor': servidor,
        'fecha': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'bases': {},
        'jobs': [],
        'avisos': [],
    }
    extraccion['sistema'] = [r['name'] for r in leer(cursor, SISTEMA)]

    for pedida in args.bases:
        fila = leer(cursor, "SELECT name, HAS_DBACCESS(name) AS acceso FROM sys.databases "
                            "WHERE name = ?", pedida)
        if not fila:
            sys.exit(f'Database "{pedida}" does not exist on {servidor}.')
        if not fila[0]['acceso']:
            sys.exit(f'Your login has no access to database "{pedida}".')
        base = fila[0]['name']
        bd = entre_corchetes(base)
        datos = {}
        print(f'\n{base}')
        for clave, sql in CONSULTAS.items():
            inicio = time.perf_counter()
            try:
                datos[clave] = leer(cursor, sql.format(bd=bd))
            except Exception as e:  # noqa: BLE001 - se informa y se sigue
                if clave in OPCIONALES:
                    extraccion['avisos'].append(f'{base}: could not read {clave} ({e})')
                    datos[clave] = []
                    continue
                sys.exit(f'Error reading {clave} from {base}: {e}')
            print(f'  {clave:<13} {len(datos[clave]):>7}  ({time.perf_counter() - inicio:.1f} s)')
        sin_definicion = sum(1 for m in datos['modulos'] if m['definition'] is None)
        if sin_definicion:
            extraccion['avisos'].append(
                f'{base}: {sin_definicion} modules without visible code (encrypted or no '
                f'VIEW DEFINITION permission): their writes and dynamic SQL are not analysed.')
        extraccion['bases'][base] = datos

    if not args.sin_jobs:
        try:
            extraccion['jobs'] = leer(cursor, JOBS)
            print(f'\nSQL Agent jobs: {len(extraccion["jobs"])} steps')
        except Exception as e:  # noqa: BLE001
            extraccion['avisos'].append(f'Could not read the jobs in msdb ({e}). '
                                        'The SQLAgentReaderRole role (or similar) is needed.')
            print('\nSQL Agent jobs: no permission on msdb, skipped')
    conexion.close()
    return extraccion


def escribir_html(datos, salida, plantilla='visor.html'):
    nombre_plantilla = plantilla
    plantilla = Path(__file__).with_name(nombre_plantilla).read_text(encoding='utf-8')
    # "<" escapado: el código de un procedimiento no puede cerrar el <script>.
    carga = json.dumps(datos, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
    if plantilla.count('__DATOS__') != 1:
        sys.exit(f'{nombre_plantilla} is missing the __DATOS__ marker')
    salida.write_text(plantilla.replace('__DATOS__', carga), encoding='utf-8')


# Cada cosa en su carpeta, junto a los scripts (no donde se ejecute el comando).
CARPETA = Path(__file__).resolve().parent
EXTRACCIONES = CARPETA / 'extractions'
MAPAS = CARPETA / 'maps'
COMPARACIONES = CARPETA / 'comparisons'


def buscar_extraccion(ruta):
    """La ruta tal cual o, si no existe, ese nombre dentro de extractions/."""
    if ruta.exists():
        return ruta
    dentro = EXTRACCIONES / ruta.name
    if dentro.exists():
        return dentro
    sys.exit(f'{ruta} not found (also looked in {EXTRACCIONES})')


def en_carpeta(ruta, carpeta):
    """Fichero de salida: un nombre suelto va a su carpeta, que se crea si no existe."""
    if ruta.parent == Path('.'):
        ruta = carpeta / ruta
    ruta.parent.mkdir(parents=True, exist_ok=True)
    return ruta


def nombre_salida(servidor, bases):
    texto = f"map_{servidor}_{'_'.join(bases)}_{datetime.now():%Y%m%d}"
    return Path(re.sub(r'[^\w.-]+', '-', texto) + '.html')


NOMBRES_TIPO = {
    'TABLA': 'tables', 'VISTA': 'views', 'PROC': 'procedures', 'FUNCION': 'functions',
    'TRIGGER': 'triggers', 'SINONIMO': 'synonyms', 'SECUENCIA': 'sequences', 'JOB': 'jobs',
    'EXTERNO': 'in other databases', 'NO_EXISTE': 'missing',
}


def main():
    # The Spanish option names still work as aliases.
    p = argparse.ArgumentParser(description='SQL Server dependency map (read only).')
    p.add_argument('--server', '--servidor', dest='servidor', metavar='SERVER', help=r'Server or server\instance')
    p.add_argument('--databases', '--bases', dest='bases', metavar='DATABASE', nargs='+', help='One or more databases')
    p.add_argument('--from', '--desde', dest='desde', metavar='FILE.json', type=Path,
                   help='Instead of connecting, use the .json written by extraer.ps1 (servers without Python)')
    p.add_argument('--user', '--usuario', dest='usuario', metavar='LOGIN', help='SQL login. Without it, Windows authentication')
    p.add_argument('--password', '--contrasena', dest='contrasena', metavar='PASSWORD', help='If not given, it is asked for without echoing')
    p.add_argument('--driver', help='Specific ODBC driver (default: the newest installed)')
    p.add_argument('--output', '--salida', dest='salida', metavar='FILE.html', type=Path, help='Output .html file')
    p.add_argument('--json', action='store_true', help='Also save the data as .json')
    p.add_argument('--no-code', '--sin-codigo', dest='sin_codigo', action='store_true',
                   help='Do not include the module code in the map (lighter)')
    p.add_argument('--no-jobs', '--sin-jobs', dest='sin_jobs', action='store_true', help='Do not read SQL Agent jobs')
    p.add_argument('--timeout', type=int, default=600, help='Seconds per query (600)')
    args = p.parse_args()
    if not args.desde and not (args.servidor and args.bases):
        p.error('--server and --databases are required, or --from with the .json from extraer.ps1')

    inicio = time.perf_counter()
    if args.desde:
        desde = buscar_extraccion(args.desde)
        extraccion = json.loads(desde.read_text(encoding='utf-8-sig'))
        print(f'Read {desde.name}: {", ".join(extraccion["bases"])} from {extraccion["servidor"]}, '
              f'{extraccion["fecha"]}')
    else:
        extraccion = extraer(args)
        # Same file extraer.ps1 writes: rebuilds the map without connecting, and feeds comparar.py.
        crudo = en_carpeta(Path(re.sub(r'[^\w.-]+', '-', f"extraction_{extraccion['servidor']}_"
                                       f"{'_'.join(extraccion['bases'])}_{datetime.now():%Y%m%d}") + '.json'),
                           EXTRACCIONES)
        crudo.write_text(json.dumps(extraccion, ensure_ascii=False,
                                    default=lambda v: v.strftime('%Y-%m-%d')), encoding='utf-8')
        print(f'\nExtraction saved: {crudo}')
    print('\nAnalysing the code...')
    datos = construir(extraccion, incluir_codigo=not args.sin_codigo)
    salida = en_carpeta(args.salida or nombre_salida(extraccion['servidor'], list(extraccion['bases'])), MAPAS)
    escribir_html(datos, salida)
    if args.json:
        salida.with_suffix('.json').write_text(
            json.dumps(datos, ensure_ascii=False, indent=1), encoding='utf-8')

    m = datos['meta']
    tipos = ', '.join(f'{v} {NOMBRES_TIPO.get(k, k.lower())}'
                      for k, v in sorted(m['porTipo'].items(), key=lambda x: -x[1]))
    print(f'\n{m["nodos"]} objects ({tipos})')
    print(f'{m["aristas"]} relationships, {m["posibles"]} of them possible (dynamic SQL)')
    for aviso in m['avisos']:
        print(f'WARNING: {aviso}')
    tam = salida.stat().st_size / 1_048_576
    print(f'\nDone in {time.perf_counter() - inicio:.0f} s: {salida.resolve()} ({tam:.1f} MB)')


if __name__ == '__main__':
    main()
