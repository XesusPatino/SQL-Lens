# CLAUDE.md

Contexto para retomar el trabajo en SQL-Lens.

## Qué es

Herramienta interna para bases de SQL Server (la principal es MESDB, de un MES
con unos 2.700 procedimientos, 550 triggers y 330 tablas). Responde dos
preguntas antes de tocar nada:

- **Si cambio este objeto, ¿qué se rompe?** → el **mapa** de dependencias.
- **¿En qué se diferencian dos servidores (DEV, SIT, PRO…)?** → la **comparación**.

Las bases viven en máquinas virtuales a las que el usuario entra por escritorio
remoto. **Las VMs no tienen Python** y no se puede instalar nada: por eso la
extracción es un script de PowerShell, y el resto (mapa y comparación) se hace
en el PC sobre un `.json`.

## Estructura

```
map.py  comparar.py  app.py     Lo que se ejecuta (comandos y app). Se quedan en la raíz
                                para que los comandos del README no cambien.
extraer.ps1                     La extracción (se copia a la VM). PowerShell 5.1, solo ASCII.
build_exe.bat                   Genera SQL-Lens.exe con PyInstaller.
core/analisis.py                Lee el T-SQL con expresiones regulares (sin parser completo).
core/grafo.py                   Construye nodos y aristas a partir de la extracción.
core/web.py                     Lee los .js/.css de los formularios de Work Tasks (regex).
templates/visor.html            Pantalla del mapa (plantilla con __DATOS__).
templates/comparar.html         Pantalla de la comparación (plantilla con __DATOS__).
templates/app.html              Ventana de la app: pestañas Extract · Map · Compare.
assets/icon.ico                 Icono del .exe y de la ventana (generado de watermark2.png).
extractions/ maps/ comparisons/ Datos del usuario. Fuera de git (ver Privacidad).
```

## El recorrido

1. **Extraer** (`extraer.ps1`, en la VM, o la pestaña Extract de la app): lanza
   SELECT de solo lectura sobre el catálogo y escribe `extraction_<servidor>_<bases>_<fecha>.json`.
   Consultas: `sys.objects`, `sys.sql_modules` (código), `sys.partitions` (filas),
   `sys.sql_expression_dependencies`, `sys.synonyms`, `sys.columns` (+ `sys.types`),
   los jobs de `msdb` y `sys.system_objects`. **Nunca lee datos de tablas.**
   Las mismas consultas están en `map.py` (`CONSULTAS`) para la conexión directa
   con pyodbc: **si cambias unas, cambia las otras.**
   Con `-Web carpeta1,carpeta2` guarda además los `.js` y `.css` de esas carpetas
   (recursivo). En AVEVA Work Tasks están en
   `C:\Program Files\AVEVA\Work Tasks\Web\BPMUITemplates\Default\NextGenForms\{js,css}`
   (lo editado por ellos, en `custom\`). `map.py --web` hace lo mismo en el PC
   (`leer_web`), también sobre una extracción ya hecha (`--from`).
   Hubo una opción `-WorkTasks` que leía los lookups y formularios de Work Tasks
   (`SKELookup`, `SKEForm`, `SKEFormDefinition` de MDMRepositoryDB). **Se quitó**:
   al usuario no le aportaba. Las extracciones viejas con su bloque `worktasks`
   siguen abriéndose: se ignora.
2. **Mapa** (`map.py --from fichero.json`): `core.grafo.construir()` → datos →
   `html_de()` mete el JSON en `templates/visor.html` → un `.html` autónomo.
3. **Comparar** (`comparar.py A.json B.json`): empareja por tipo + `esquema.nombre`,
   compara código (ignora espacios) y columnas de tablas, guarda solo el *diff*.
   Los `.js`/`.css` se emparejan por su ruta. En `comparar.html`, un `<select>` de
   tipo junto a «Filter by name» (como el del mapa), sincronizado con las cifras de
   la tabla de resumen, que también filtran por tipo y estado al pulsarlas.

**Dentro de `extractions/`, una carpeta por tipo de extracción**, para comparar
solo las del mismo tipo (lo pidió el usuario): `extractions/extractions_db` (solo
la base) y `extractions/extractions_wf` (+ `-Web`). Están en `TIPOS_EXTRACCION` / `CARPETAS_EXTRACCION`
(`map.py`). La app y `map.py` eligen la carpeta según las opciones. `extraer.ps1`
pone el tipo al final del nombre (`extraction_<servidor>_<bases>_wf_<fecha>.json`)
para saber dónde copiarlo, y los mapas y comparaciones guardados llevan el mismo
sufijo (`sufijo_tipo`): si no, dos del mismo día se pisarían. Los `.json` sueltos
en `extractions/` (como estaban antes) se siguen leyendo: grupo «Not sorted».

Los nombres de fichero sueltos se buscan en todas esas carpetas; las salidas van a
`maps/` y `comparisons/` (`buscar_extraccion`, `en_carpeta` en `map.py`). Las
carpetas son relativas al script, o al `.exe` cuando está compilado
(`sys.frozen`): nunca a la carpeta temporal de PyInstaller.

## Formato del .json de extracción

```
{"servidor", "fecha", "sistema": [nombres del sistema],
 "bases": {"MESDB": {"objetos", "modulos", "filas", "dependencias", "sinonimos", "columnas"}},
 "jobs": [pasos], "web": [{"ruta": "js/custom/x.js", "fecha", "codigo"}], "avisos": [texto]}
```

`web` solo está si se extrajo con `-Web`. La `ruta` empieza por el nombre de la
carpeta pasada: para comparar dos servidores, extraer los dos con las mismas carpetas.

Lo escribe `extraer.ps1` a mano (ConvertTo-Json de PS 5 es lento y se queda
corto de profundidad) y fila a fila al disco. Las fechas van como `yyyy-MM-dd`.
Las extracciones antiguas (`extraccion_…`, sin `columnas`) siguen valiendo:
la comparación avisa de que las tablas solo se comparan por nombre.

## El grafo (core/grafo.py)

- Arista `A → B` = "A usa a B". Tipos: `lee`, `escribe`, `ejecuta`, `usa`
  (funciones, secuencias, DEFAULT/CHECK de una tabla), `dispara` (trigger y su
  tabla), `sinonimo`. Flag `posible` = deducido de SQL dinámico.
- Claves de nodo en minúsculas: `base.esquema.nombre`; jobs: `job:nombre`.
- `resolver()`: sin esquema → `dbo` → único con ese nombre. Otra base no
  extraída → nodo `EXTERNO`. No existe → `NO_EXISTE` (referencias rotas).
  Se descartan: objetos del sistema, `inserted`/`deleted` (pseudo-tablas de
  triggers) y `alias.metodo` de XML (`c.value(...)`: esquema que no existe).
  Antes de estos filtros salían ~570 relaciones falsas.
- **Lee o escribe** sale de `core/analisis.py`: quita comentarios y literales,
  busca INSERT/UPDATE/DELETE/MERGE/INTO/TRUNCATE/ALTER, y resuelve alias
  (`UPDATE t … FROM dbo.Tabla t`) con UNA pasada por módulo (`_Alias`). La
  versión con una regex por alias tardaba 92 s en MESDB; ahora ~2 s. Además la
  vieja tomaba `AS` de `AFTER INSERT, UPDATE, DELETE AS` como alias.
- **Ficheros web** (`cargar_web`): nodos `JS`/`CSS` con clave `web:<ruta>` y
  `LOOKUP` con clave `lookup:<nombre>`. `core/web.py` quita comentarios y cadenas
  (con cuidado de las `/regex/` y las plantillas `` `${}` ``) y busca:
  - `*DirectAccess(tipo, "sp")` → `ejecuta` (si la SP no existe, `NO_EXISTE`).
    Con la SP en una variable, se buscan las cadenas que se le asignan en el
    fichero (`x = "sp"`, `x = c ? "a" : "b"`; sin mirar ámbitos). Si no hay
    (concatenada, parámetro de función), el nodo lleva `d`.
  - Cualquier cadena que sea el nombre de una **SP** → `ejecuta`. Solo SPs: hay
    tablas `job`, `item`, `line`… que saldrían de cualquier texto del JS.
  - `*Lookup*("nombre")` → `usa` el LOOKUP. Una cadena que acabe en `Lookup`
    también. Y `getLookup*(variable)` (`var lookupName = "ofi_lm_mes_attribute"`),
    igual que con las SPs. Solo `getLookup*`: `SFU.getUserLookupExtendedInformation(x)`
    y las librerías tienen funciones "lookup" que no reciben un lookup (con
    `get*Lookup*` salían 14 JS «dinámicos» de más). Del lookup solo se sabe quién
    lo llama, no qué lee.
  - Cadenas que empiezan por SELECT/INSERT/… → `referencias_en_texto`.
  - Funciones llamadas que se definen en otro fichero (en 1, o en ≤3 como
    `posible`; sin los nombres de `COMUNES`) y nombres de `.js`/`.css` → `usa`.
  Las cadenas que son solo un nombre enlazan con SPs, vistas y funciones; con
  tablas, solo si llevan esquema. Las SPs se buscan primero en MESDB.

## El filtro por tipo (templates/visor.html, `estado.filtro`)

Un `<select>` junto al buscador con los tipos que hay en el mapa y cuántos de cada uno
(`pasaFiltro`). Con un tipo elegido:

- El buscador solo encuentra ese tipo.
- El inicio cambia "Most used" y "Broken references" por la lista entera de ese
  tipo, los más usados primero.
- En un objeto, la lista de lo afectado (y *Copy list for Excel*) y "What it uses"
  solo muestran ese tipo; la frase sigue diciendo el total de todos los tipos.
- En el dibujo, los de otros tipos solo se atenúan (`.nodo.fuera`): hacen falta
  para seguir el camino hasta los que sí son del tipo.

Sustituye a la vista «cerebro» (todo el grafo en un canvas con fuerzas y grupos
Louvain), que se quitó porque al usuario no le servía.

## La app (app.py)

- Servidor HTTP propio en `127.0.0.1:47821` (o un puerto libre) + ventana.
  Sin `SO_REUSEADDR` (clase `Servidor`): en Windows deja a un segundo SQL-Lens
  escuchar en el mismo puerto, y sus peticiones llegaban al primero (403).
  Sin él, reabrir el 47821 justo después de cerrar sigue funcionando.
- **Ventana propia con pywebview** (WebView2, `gui='edgechromium'`). Se hizo así
  porque con Edge en modo `--app` el icono de la barra de tareas salía borroso
  (lo pinta Edge y Windows lo reescala). **Plan B**: si pywebview o WebView2
  fallan, abre Edge `--app` y un vigía cierra el servidor cuando la página deja
  de enviar latidos (`/api/ping` cada 4 s; 150 s de margen por el
  estrangulamiento de temporizadores con la ventana minimizada).
- **Sin WebView2** (Windows Server sin el runtime, o .NET < 4.6.2) pywebview usa
  sin avisar el motor de Internet Explorer: la página sale sin colores (no hay
  `var()`) y sin JavaScript. Pasó en una VM de dev. Por eso `ventana_propia` mira
  `webview.platforms.winforms.renderer` y, si no es `edgechromium`, va al plan B.
  Si tampoco hay Edge ni Chrome, abre el navegador por defecto; si es IE, la
  página muestra `#viejo` (solo lo ve IE: lo oculta `@supports`), que manda a
  usar `extraer.ps1`. Para ver la página con el motor de IE: un
  `System.Windows.Forms.WebBrowser` en PowerShell 5.1 con `X-UA-Compatible: IE=edge`.
- En una VM de ofi, el EDR de la empresa puso el `.exe` en cuarentena
  («malicious behavior»). La compilación anterior le funcionaba. Un onefile de
  PyInstaller sin firmar se descomprime en `%TEMP%` y se ejecuta desde ahí, y
  Extract lanza PowerShell oculto con `-ExecutionPolicy Bypass`: es lo que hace
  el malware, y cada compilación es un fichero nuevo, sin reputación. El
  Defender del PC no lo detecta. La salida buena es que IT lo autorice (o firmar
  el `.exe`), no retocarlo hasta que pase. Mientras, en esas VMs, `extraer.ps1`.
- **Seguridad**: token aleatorio por arranque (`?t=` o cabecera `X-Token`),
  comprobación de `Host`, y solo ficheros de las carpetas de extracciones: el
  navegador manda `carpeta/nombre`, la carpeta tiene que ser una de
  `TODAS_EXTRACCIONES` y del nombre solo vale la última parte (nada de `..`).
  Única ruta sin token: los iconos (`/favicon.ico`, `/icon-256.png`).
- **Extract** ejecuta el `extraer.ps1` empaquetado con PowerShell (`-Command`,
  salida en UTF-8) en la carpeta de su tipo, y lee la línea `Done in … s: <ruta>`.
  Map y Compare listan las extracciones agrupadas por carpeta (`<optgroup>`), y
  B elige por defecto otra del mismo tipo que A. *Open extractions folders* abre
  `extractions/` y crea dentro las dos de tipo.
  Por eso **no necesita pyodbc ni drivers ODBC**. Solo autenticación de Windows.
  Casilla *Web files* (carpetas, una por línea; por defecto las de NextGenForms)
  → `-Web`. `app.py` comprueba antes las carpetas: se leen en el equipo donde
  corre la app (la VM, o una ruta `\\servidor\c$\…`).
- Map y Compare se generan al vuelo (caché de 6) y se muestran en un iframe.
  *Save as HTML* los escribe en `maps/` / `comparisons/`.
- Tema claro/oscuro arriba a la derecha: `data-theme` en la app y en los
  iframes. Se guarda en localStorage (`sql-lens-tema`), persistente porque
  pywebview arranca con `private_mode=False` y datos en `%LOCALAPPDATA%\SQL-Lens`.
- Log solo de errores: `sql-lens.log` junto al `.exe`.
- `python app.py --no-window` arranca solo el servidor e imprime la URL: es la
  forma de probar sin abrir ventanas.

## Compilar

```
pip install pyinstaller pillow pywebview
.\build_exe.bat          (en PowerShell hace falta el .\ delante)
```

Onefile, `--windowed`, icono `assets\icon.ico`, empaqueta `templates/`,
`assets/icon.ico` y `extraer.ps1`. Trabajo en `%TEMP%\sql-lens-build` para no
ensuciar la carpeta. ~15 MB. **Cierra SQL-Lens antes**: Windows no deja
sobrescribir un `.exe` en marcha. Tras cambiar el código hay que recompilar.

## Cómo probar sin SQL Server

- `python map.py --from <extracción>` y `python comparar.py A.json B.json` con
  las extracciones reales de `extractions/extractions_db/` (usar `--output` a una ruta temporal
  para no pisar los ficheros del usuario). Para probar otros tipos, crear una
  extracción de prueba en `extractions/extractions_wf`… y borrarla después. La carpeta
  del proyecto está en OneDrive: las carpetas vacías solo se borran con `Remove-Item -Force`.
- `app.py --no-window` (o el `.exe --no-window`) + `curl` a `/`, `/favicon.ico`,
  `/icon-256.png`, `/view/map?file=…&t=…`, `/view/compare?a=…&b=…&t=…`.
- Extract contra un servidor inexistente (`127.0.0.1,1`) comprueba que el
  `.ps1` se lanza y que el error llega a la ventana (tarda ~30 s).
- Para una comparación con diferencias conocidas: copiar una extracción y
  quitar/añadir/modificar objetos a mano.
- `extraer.ps1` con `powershell -File` (desde cmd o desde PowerShell 5.1): una
  lista `a,b` o `"a","b"` llega como UN solo texto `a,b`, a veces con las comillas.
  Desde PowerShell 7 pueden llegar como argumentos sueltos. Por eso el script
  parte `-Databases`/`-Web` por las comas (salvo una carpeta que exista con coma
  en el nombre) y usa `PositionalBinding = $false`: antes, la carpeta sobrante podía
  acabar en `-User`. La app usa `-Command`, donde las listas llegan bien. Probar
  las cuatro formas: cmd `-File`, PS 5.1 `-File`, PS 7 `-File` y `-Command`.
- `extraer.ps1`: comprobar sintaxis con el parser de PowerShell 5.1
  (`[System.Management.Automation.Language.Parser]::ParseFile`) y que siga
  siendo solo ASCII (PS 5.1 lee mal los .ps1 UTF-8 sin BOM).
- **Al terminar una prueba**, borrar lo que se haya generado (logs, mapas o
  comparaciones de prueba) y no matar procesos `python.exe` en general: solo
  el que se lanzó.

## Privacidad (importante)

El repositorio de GitHub es **público**. Las extracciones, mapas y
comparaciones llevan el **código de todos los procedimientos** de bases de la
empresa: **nunca deben entrar en git**. Están en `.gitignore` (`extractions/`,
`extractions_*/`, `maps/`, `comparisons/`, `*.json`, `*.exe`, `*.log`, `.vscode/`). Antes de
cualquier commit, `git status` para comprobar que no se cuela ninguno, y no
poner nombres de servidores ni datos de la empresa en ficheros versionados.

## Preferencias del usuario

- **Todo lo que se ve, en inglés** (pantallas, mensajes, README). Los
  comentarios del código, en español. Los nombres de funciones y variables ya
  están en español; seguir igual.
- Le gusta **poco ruido**: pantallas informativas sin sobrecarga, README corto,
  la raíz ordenada. Ha retocado él mismo el README y el estilo: no reescribirlo
  sin que lo pida; cambios mínimos y avisar.
- El icono es su `watermark2.png` (logo azul sobre círculo blanco). Se probaron
  versiones redibujadas y no le gustaron.
- Trabaja en Windows, con VS Code (terminal PowerShell). Hay copias de la
  carpeta (`SQL-Lens_Light`, `SQL-Lens_Test`): cada una es independiente.

## Limitaciones conocidas

- No ve aplicaciones, informes ni paquetes SSIS: un objeto sin dependientes
  puede estar en uso desde fuera.
- SQL dinámico: aproximado (aristas `posible`); si el nombre se construye por
  partes, no se puede saber.
- "Lee o escribe" es heurístico; la relación en sí viene del catálogo.
- La extracción en la app solo admite autenticación de Windows.
- La extracción real desde la app no se ha probado contra un SQL Server desde
  el PC (no hay acceso); el `.ps1` sí funciona en las VMs.
