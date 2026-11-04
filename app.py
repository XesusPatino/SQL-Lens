"""
SQL-Lens as a desktop app: one window with three tabs (Extract, Map, Compare).

A small web server on 127.0.0.1 serves app.html and builds the map and the
comparison on demand. The window is the program's own (pywebview, on the
WebView2 that Windows 10/11 already has), or Edge in "app" mode if that is not
available, so nothing has to be installed. build_exe.bat packages it as
"SQL-Lens.exe"; it also runs as `python app.py` (`--no-window` only prints the
address, for testing).
"""

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import comparar
from map import (CARPETA, CARPETAS_EXTRACCION, COMPARACIONES, EXTRACCIONES, MAPAS, TIPOS_EXTRACCION,
                 TODAS_EXTRACCIONES, html_de, nombre_salida, tipo_extraccion)
from core.grafo import construir

# Plantillas y extraer.ps1: junto al script o, en el .exe, en su carpeta temporal.
RECURSOS = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
TOKEN = secrets.token_urlsafe(24)
PUERTO_PREFERIDO = 47821   # fijo si está libre: así el navegador recuerda las preferencias
LOG = CARPETA / 'sql-lens.log'   # solo errores
SIN_VENTANA = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def registrar(texto):
    try:
        with LOG.open('a', encoding='utf-8') as f:
            f.write(f'{datetime.now():%Y-%m-%d %H:%M:%S}  {texto}\n')
    except OSError:
        pass


class ErrorUsuario(Exception):
    """Algo que se le explica a quien usa la app (no un fallo del programa)."""


def _mensaje(e):
    # Los scripts de línea de comandos terminan con sys.exit('mensaje').
    return str(e.code) if isinstance(e, SystemExit) else str(e)


# EXTRACCIONES:

def lista_extracciones():
    """Las de todas las carpetas (una por tipo); cada una se identifica por «carpeta/nombre»."""
    items = [{'id': f'{carpeta.name}/{p.name}', 'folder': carpeta.name, 'name': p.name,
              'size': p.stat().st_size, 'modified': p.stat().st_mtime}
             for carpeta in TODAS_EXTRACCIONES if carpeta.exists()
             for p in carpeta.glob('*.json') if p.is_file()]
    return sorted(items, key=lambda x: -x['modified'])


def extraccion(ident):
    """Solo ficheros de las carpetas de extracciones: «carpeta/nombre» llega del navegador."""
    carpeta, _, nombre = (ident or '').rpartition('/')
    base = {c.name: c for c in TODAS_EXTRACCIONES}.get(carpeta)
    ruta = base / Path(nombre).name if base else None
    if not ruta or ruta.suffix.lower() != '.json' or not ruta.is_file():
        raise ErrorUsuario(f'"{nombre or ident}" is not in the extractions folders any more. Refresh the list.')
    return ruta



# MAPA Y COMPARE:

_cache, _cache_lock = {}, threading.Lock()


def en_cache(clave, fabrica):
    with _cache_lock:
        if clave in _cache:
            return _cache[clave]
    valor = fabrica()
    with _cache_lock:
        _cache[clave] = valor
        while len(_cache) > 6:
            _cache.pop(next(iter(_cache)))
    return valor


def mapa(ruta):
    def fabrica():
        try:
            ex = json.loads(ruta.read_text(encoding='utf-8-sig'))
            return (html_de(construir(ex), 'visor.html'),
                    nombre_salida(ex['servidor'], list(ex['bases']), tipo_extraccion(ex)))
        except (ValueError, KeyError) as e:
            raise ErrorUsuario(f'{ruta.name} is not an extraction file ({e}).')
        except SystemExit as e:
            raise ErrorUsuario(_mensaje(e))
    # Por la ruta entera: el mismo nombre puede estar en dos carpetas.
    return en_cache(('map', str(ruta), ruta.stat().st_mtime), fabrica)


def comparacion(ruta_a, ruta_b):
    def fabrica():
        try:
            datos, a, b = comparar.preparar(ruta_a, ruta_b)
            return html_de(datos, 'comparar.html'), comparar.nombre_comparacion(a, b)
        except (ValueError, KeyError) as e:
            raise ErrorUsuario(f'One of the files is not an extraction file ({e}).')
        except SystemExit as e:
            raise ErrorUsuario(_mensaje(e))
    clave = ('compare', str(ruta_a), ruta_a.stat().st_mtime, str(ruta_b), ruta_b.stat().st_mtime)
    return en_cache(clave, fabrica)


# EXTRACCIÓN:

class Trabajo:
    def __init__(self):
        self.lineas = []
        self.estado = 'running'   # running · done · error
        self.fichero = None


TRABAJOS = {}


def lanzar_extraccion(servidor, bases, web=None):
    """web: carpetas de .js/.css (-Web), o None."""
    servidor = (servidor or '').strip()
    bases = [b.strip() for b in (bases or '').split(',') if b.strip()]
    if not servidor or not bases:
        raise ErrorUsuario('Write the server and at least one database.')
    carpetas = []
    if web is not None:
        carpetas = [c.strip().strip('"') for c in web if isinstance(c, str) and c.strip().strip('"')]
        if not carpetas:
            raise ErrorUsuario('Write at least one web folder, or untick Web files.')
        # Se leen en este equipo (como haría extraer.ps1): en la VM, o por una ruta compartida.
        for c in carpetas:
            if not Path(c).is_dir():
                raise ErrorUsuario(f'The folder "{c}" does not exist on this computer. Web files are read where '
                                   'SQL-Lens runs: run it on the VM, or use a shared path (\\\\server\\c$\\...).')
    # Cada tipo en su carpeta (extractions_db, extractions_wf), para comparar las del mismo tipo.
    destino = CARPETAS_EXTRACCION[TIPOS_EXTRACCION[bool(carpetas)]]
    destino.mkdir(parents=True, exist_ok=True)

    def q(texto):   # cadena literal de PowerShell
        return "'" + texto.replace("'", "''") + "'"

    comando = ('[Console]::OutputEncoding = [Text.Encoding]::UTF8; '
               f"& {q(str(RECURSOS / 'extraer.ps1'))} -Server {q(servidor)} "
               f"-Databases {','.join(q(b) for b in bases)}"
               + (f" -Web {','.join(q(c) for c in carpetas)}" if carpetas else '')
               + '; exit $LASTEXITCODE')
    proceso = subprocess.Popen(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', comando],
        cwd=destino, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=SIN_VENTANA)
    trabajo = Trabajo()
    ident = secrets.token_hex(6)
    TRABAJOS[ident] = trabajo

    def seguir():
        for crudo in proceso.stdout:
            linea = crudo.decode('utf-8', errors='replace').rstrip()
            trabajo.lineas.append(linea)
            m = re.match(r'Done in .*? s: (.+?\.json) \(', linea)
            if m:
                trabajo.fichero = f'{destino.name}/{Path(m.group(1)).name}'
        proceso.wait()
        if proceso.returncode == 0 and trabajo.fichero:
            trabajo.estado = 'done'
        else:
            trabajo.estado = 'error'
            registrar(f'Extract failed ({proceso.returncode}): ' + ' | '.join(trabajo.lineas[-5:]))

    threading.Thread(target=seguir, daemon=True).start()
    return ident



# Cerrar solo: la ventana avisa cada pocos segundos de que sigue abierta

class Vigia:
    def __init__(self):
        self.ultimo = time.time()
        self.visto = False

    def latido(self):
        self.ultimo = time.time()
        self.visto = True

    def adios(self):
        # Al cerrar o recargar: si en 8 s no vuelve a latir, se cierra.
        self.ultimo = time.time() - self.limite() + 8

    def limite(self):
        # Con la ventana minimizada, el navegador espacia los temporizadores hasta 1 por minuto.
        return 150 if self.visto else 90


VIGIA = Vigia()


def vigilar(servidor):
    while True:
        time.sleep(2)
        ocupado = any(t.estado == 'running' for t in TRABAJOS.values())
        if not ocupado and time.time() - VIGIA.ultimo > VIGIA.limite():
            servidor.shutdown()
            return


# HTTP

PAGINA_ERROR = """<!doctype html><html lang="en"><meta charset="utf-8"><title>SQL-Lens</title>
<style>:root{{--bg:#f4f5f8;--fg:#1b2130;--muted:#5f6879;--line:#e0e3e9}}
@media (prefers-color-scheme:dark){{:root{{--bg:#11141a;--fg:#e5e8ee;--muted:#9ba3b4;--line:#2a303c;color-scheme:dark}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,"Segoe UI",sans-serif;display:grid;place-items:center;min-height:100vh}}
div{{max-width:560px;padding:24px;border-left:3px solid #d03b3b}}p{{color:var(--muted);margin:6px 0 0}}</style>
<div><strong>{titulo}</strong><p>{texto}</p></div></html>"""


def icono_grande():
    """La imagen de 256 px que lleva dentro icon.ico (cada tamaño es un PNG)."""
    datos = (RECURSOS / 'assets' / 'icon.ico').read_bytes()
    for i in range(int.from_bytes(datos[4:6], 'little')):
        entrada = datos[6 + 16 * i:22 + 16 * i]
        if entrada[0] == 0:   # en un .ico, ancho 0 significa 256
            tamano = int.from_bytes(entrada[8:12], 'little')
            inicio = int.from_bytes(entrada[12:16], 'little')
            return datos[inicio:inicio + tamano]
    raise ErrorUsuario('icon.ico has no 256 px image.')


def _escapar(texto):
    return str(texto).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


class Manejador(BaseHTTPRequestHandler):
    server_version = 'SQL-Lens'

    def log_message(self, *args):   # sin consola en el .exe
        pass

    # Respuestas
    def _enviar(self, codigo, cuerpo, tipo):
        datos = cuerpo.encode('utf-8') if isinstance(cuerpo, str) else cuerpo
        self.send_response(codigo)
        self.send_header('Content-Type', tipo if tipo.startswith('image/') else f'{tipo}; charset=utf-8')
        self.send_header('Content-Length', str(len(datos)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(datos)

    def _json(self, valor, codigo=200):
        self._enviar(codigo, json.dumps(valor), 'application/json')

    def _pagina_error(self, texto, codigo=400):
        self._enviar(codigo, PAGINA_ERROR.format(titulo='Could not open it', texto=_escapar(texto)), 'text/html')

    # Seguridad: solo esta ventana
    def _permitido(self, consulta):
        host_ok = self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}',
                                               f'localhost:{self.server.server_port}')
        token = consulta.get('t', [''])[0] or self.headers.get('X-Token', '')
        return host_ok and secrets.compare_digest(token, TOKEN)

    def do_GET(self):
        url = urlparse(self.path)
        consulta = parse_qs(url.query)
        if url.path == '/favicon.ico':
            # El icono de la ventana: el navegador lo pide por su cuenta, sin la clave. Solo es una imagen.
            return self._enviar(200, (RECURSOS / 'assets' / 'icon.ico').read_bytes(), 'image/x-icon')
        if url.path == '/icon-256.png':
            # El de 256 px para la barra de tareas: Edge lo reduce en vez de ampliar el de 16 (que se ve borroso).
            return self._enviar(200, icono_grande(), 'image/png')
        if not self._permitido(consulta):
            return self._enviar(403, 'Forbidden', 'text/plain')
        arg = lambda k: consulta.get(k, [''])[0]   # noqa: E731
        try:
            if url.path == '/':
                return self._enviar(200, (RECURSOS / 'templates' / 'app.html').read_text(encoding='utf-8'), 'text/html')
            if url.path == '/api/ping':
                VIGIA.latido()
                return self._json({'ok': True})
            if url.path == '/api/extractions':
                return self._json({'items': lista_extracciones()})
            if url.path == '/api/job':
                trabajo = TRABAJOS.get(arg('id'))
                if not trabajo:
                    return self._json({'error': 'Unknown extraction.'}, 404)
                desde = int(arg('from') or 0)
                return self._json({'state': trabajo.estado, 'lines': trabajo.lineas[desde:],
                                   'next': len(trabajo.lineas), 'file': trabajo.fichero})
            if url.path == '/view/map':
                return self._enviar(200, mapa(extraccion(arg('file')))[0], 'text/html')
            if url.path == '/view/compare':
                return self._enviar(200, comparacion(extraccion(arg('a')), extraccion(arg('b')))[0], 'text/html')
            return self._enviar(404, 'Not found', 'text/plain')
        except ErrorUsuario as e:
            if url.path.startswith('/view/'):
                return self._pagina_error(e)
            return self._json({'error': str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            registrar(traceback.format_exc())
            if url.path.startswith('/view/'):
                return self._pagina_error(f'Unexpected error: {e}. Details in sql-lens.log.', 500)
            return self._json({'error': f'Unexpected error: {e}'}, 500)

    def do_POST(self):
        url = urlparse(self.path)
        consulta = parse_qs(url.query)
        if not self._permitido(consulta):
            return self._enviar(403, 'Forbidden', 'text/plain')
        if url.path == '/api/bye':   # sendBeacon al cerrar la ventana
            VIGIA.adios()
            return self._json({'ok': True})
        try:
            largo = int(self.headers.get('Content-Length') or 0)
            cuerpo = json.loads(self.rfile.read(largo) or b'{}')
            if url.path == '/api/extract':
                web = cuerpo.get('web')
                return self._json({'id': lanzar_extraccion(cuerpo.get('server'), cuerpo.get('databases'),
                                                           web if isinstance(web, list) else None)})
            if url.path == '/api/save':
                if cuerpo.get('kind') == 'map':
                    html, nombre = mapa(extraccion(cuerpo.get('file')))
                    carpeta = MAPAS
                else:
                    html, nombre = comparacion(extraccion(cuerpo.get('a')), extraccion(cuerpo.get('b')))
                    carpeta = COMPARACIONES
                carpeta.mkdir(parents=True, exist_ok=True)
                (carpeta / nombre).write_text(html, encoding='utf-8')
                return self._json({'path': str(carpeta / nombre), 'name': nombre.name})
            if url.path == '/api/open-folder':
                if cuerpo.get('which') == 'extractions':
                    # La que tiene todas dentro: se crean las de cada tipo, para copiar ahí las de extraer.ps1.
                    for c in CARPETAS_EXTRACCION.values():
                        c.mkdir(parents=True, exist_ok=True)
                    carpeta = EXTRACCIONES
                else:
                    carpeta = {'maps': MAPAS, 'comparisons': COMPARACIONES,
                               **{c.name: c for c in TODAS_EXTRACCIONES}}.get(cuerpo.get('which'))
                if not carpeta:
                    raise ErrorUsuario('Unknown folder.')
                carpeta.mkdir(parents=True, exist_ok=True)
                os.startfile(carpeta)  # noqa: S606 - abre el Explorador en esa carpeta
                return self._json({'ok': True})
            return self._enviar(404, 'Not found', 'text/plain')
        except ErrorUsuario as e:
            return self._json({'error': str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            registrar(traceback.format_exc())
            return self._json({'error': f'Unexpected error: {e}'}, 500)


class Servidor(ThreadingHTTPServer):
    # En Windows, SO_REUSEADDR deja a un segundo SQL-Lens escuchar en el mismo puerto que el primero, y sus
    # peticiones pueden llegar al otro (403). Sin él, el segundo usa otro puerto; reabrir el 47821 justo
    # después de cerrar sigue funcionando.
    allow_reuse_address = False


# VENTANA:

def navegador_app():
    """Edge (o Chrome) para abrir la app en su propia ventana, sin pestañas."""
    bases = [os.environ.get(v) for v in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA')]
    for relativa in (r'Microsoft\Edge\Application\msedge.exe', r'Google\Chrome\Application\chrome.exe'):
        for base in filter(None, bases):
            ruta = Path(base) / relativa
            if ruta.is_file():
                return ruta
    return None


def abrir_ventana(url):
    exe = navegador_app()
    if exe:
        subprocess.Popen([str(exe), f'--app={url}', '--window-size=1440,900', '--no-first-run'],
                         creationflags=SIN_VENTANA)
    else:
        webbrowser.open(url)


def aviso_fatal(texto):
    registrar(texto)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, texto, 'SQL-Lens', 0x10)
    except Exception:  # noqa: BLE001
        print(texto, file=sys.stderr)


def ventana_propia(url):
    """
    La ventana del propio programa (pywebview + WebView2): Windows le pone el icono
    del .exe tal cual, nítido en la barra de tareas. Devuelve False si no se puede
    (sin pywebview o sin WebView2, p. ej. en Windows Server), para abrir Edge en su lugar.
    """
    try:
        import webview
    except ImportError:
        return False
    try:
        # Sin el runtime de WebView2 (o con .NET < 4.6.2), pywebview usa sin avisar el motor de
        # Internet Explorer, que no entiende la página (sin colores ni JavaScript): mejor Edge.
        from webview.platforms import winforms
        if getattr(winforms, 'renderer', 'edgechromium') != 'edgechromium':
            registrar(f'No WebView2 on this machine (the window would use {winforms.renderer}): opening Edge instead.')
            return False
        webview.create_window('SQL-Lens', url, width=1440, height=900, min_size=(900, 600))
        # Sin modo privado, para que recuerde el tema y lo último elegido.
        datos = Path(os.environ.get('LOCALAPPDATA') or CARPETA) / 'SQL-Lens'
        webview.start(gui='edgechromium', private_mode=False, storage_path=str(datos))
        return True
    except Exception:  # noqa: BLE001
        registrar('Own window failed, opening Edge instead:\n' + traceback.format_exc())
        return False


def main():
    try:
        servidor = Servidor(('127.0.0.1', PUERTO_PREFERIDO), Manejador)
    except OSError:
        servidor = Servidor(('127.0.0.1', 0), Manejador)
    url = f'http://127.0.0.1:{servidor.server_port}/?t={TOKEN}'
    if '--no-window' in sys.argv:
        # Para pruebas: sin ventana, la dirección sale por la consola (o al log en el .exe, que no tiene).
        print(url, flush=True) if sys.stdout else registrar(url)
        threading.Thread(target=vigilar, args=(servidor,), daemon=True).start()
        servidor.serve_forever()
        return
    hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
    hilo.start()
    if ventana_propia(url):
        # Se ha cerrado la ventana: fin. Si queda una extracción a medias, PowerShell la termina sola.
        servidor.shutdown()
        return
    # Plan B: Edge en modo aplicación, y cerrar solo cuando la ventana deje de dar señales.
    threading.Thread(target=vigilar, args=(servidor,), daemon=True).start()
    abrir_ventana(url)
    hilo.join()


if __name__ == '__main__':
    try:
        main()
    except Exception:  # noqa: BLE001
        aviso_fatal('SQL-Lens could not start:\n\n' + traceback.format_exc())
