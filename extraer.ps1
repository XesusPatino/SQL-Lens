<#
.SYNOPSIS
    Reads the SQL Server catalog and saves it to a .json, to build the map or a
    comparison on another machine.

.DESCRIPTION
    For servers without Python: nothing to install, it uses the SQL Server client
    that ships with .NET. It only runs SELECT on the catalog (sys.objects,
    sys.sql_modules, sys.sql_expression_dependencies, sys.synonyms, sys.columns
    and the jobs in msdb): it never reads table data or writes anything.

    Then, on a machine with Python:
        python extraer.py --from extraction_....json
        python comparar.py extraction_A.json extraction_B.json

    The queries are the same as in extraer.py: if you change one, change both.
    The .json contains the code of every procedure: treat it accordingly.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\extraer.ps1 -Server localhost -Databases MESDB

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\extraer.ps1 -Server localhost -Databases MESDB,MESDB002
#>
param(
    [Parameter(Mandatory = $true)][Alias('Servidor')][string]$Server,
    [Parameter(Mandatory = $true)][Alias('Bases')][string[]]$Databases,
    [Alias('Usuario')][string]$User,
    [Alias('Salida')][string]$Output,
    [Alias('SinJobs')][switch]$NoJobs,
    [int]$Timeout = 600
)

$ErrorActionPreference = 'Stop'

$Queries = [ordered]@{
    objetos      = @"
        SELECT o.object_id, s.name AS esquema, o.name, o.type, o.create_date,
               o.modify_date, o.parent_object_id
        FROM {bd}.sys.objects o
        JOIN {bd}.sys.schemas s ON s.schema_id = o.schema_id
        WHERE o.is_ms_shipped = 0
"@
    modulos      = @"
        SELECT m.object_id, m.definition
        FROM {bd}.sys.sql_modules m
        JOIN {bd}.sys.objects o ON o.object_id = m.object_id
        WHERE o.is_ms_shipped = 0
"@
    filas        = @"
        SELECT p.object_id, SUM(p.rows) AS filas
        FROM {bd}.sys.partitions p
        WHERE p.index_id IN (0, 1)
        GROUP BY p.object_id
"@
    dependencias = @"
        SELECT DISTINCT d.referencing_id, d.referenced_id, d.referenced_server_name,
               d.referenced_database_name, d.referenced_schema_name,
               d.referenced_entity_name
        FROM {bd}.sys.sql_expression_dependencies d
        WHERE d.referencing_class = 1 AND d.referenced_class = 1
"@
    sinonimos    = @"
        SELECT s.object_id, s.base_object_name
        FROM {bd}.sys.synonyms s
"@
    columnas     = @"
        SELECT c.object_id, c.column_id, c.name, t.name AS tipo, c.max_length,
               c.precision, c.scale, c.is_nullable, c.is_identity
        FROM {bd}.sys.columns c
        JOIN {bd}.sys.objects o ON o.object_id = c.object_id
        JOIN {bd}.sys.types t ON t.user_type_id = c.user_type_id
        WHERE o.type = 'U' AND o.is_ms_shipped = 0
"@
}
# These can fail (permissions) without stopping the extraction.
$Optional = @('filas', 'sinonimos', 'columnas')

$QuerySystem = 'SELECT DISTINCT name FROM sys.system_objects'

$QueryJobs = @"
    SELECT j.job_id, j.name AS job, j.enabled, s.step_id, s.step_name, s.subsystem,
           s.database_name, s.command
    FROM msdb.dbo.sysjobs j
    JOIN msdb.dbo.sysjobsteps s ON s.job_id = j.job_id
    ORDER BY j.name, s.step_id
"@

# ---------------------------------------------------------------------------
# JSON written by hand: ConvertTo-Json in PowerShell 5 is slow and runs out of
# depth with thousands of rows and long procedures.
# ---------------------------------------------------------------------------
$Invariant = [System.Globalization.CultureInfo]::InvariantCulture
$ControlEvaluator = [System.Text.RegularExpressions.MatchEvaluator] {
    param($m) '\u{0:x4}' -f [int][char]$m.Value
}

function Escape-Json([string]$s) {
    $s = $s.Replace('\', '\\').Replace('"', '\"').Replace("`r", '\r').Replace("`n", '\n').Replace("`t", '\t')
    if ($s -match '[\x00-\x1f]') {
        $s = [regex]::Replace($s, '[\x00-\x1f]', $ControlEvaluator)
    }
    return '"' + $s + '"'
}

function Format-Value($v) {
    if ($null -eq $v -or $v -is [System.DBNull]) { return 'null' }
    if ($v -is [string]) { return Escape-Json $v }
    if ($v -is [bool]) { if ($v) { return 'true' } else { return 'false' } }
    if ($v -is [datetime]) { return '"' + $v.ToString('yyyy-MM-dd') + '"' }
    if ($v -is [byte] -or $v -is [int16] -or $v -is [int32] -or $v -is [int64] -or $v -is [decimal]) {
        return [System.Convert]::ToString($v, $Invariant)
    }
    return Escape-Json ([string]$v)   # guid and anything else, as text
}

function Write-Table($w, [System.Data.DataTable]$table) {
    $columns = @($table.Columns | ForEach-Object { $_.ColumnName })
    $keys = @($columns | ForEach-Object { (Escape-Json $_) + ':' })
    $w.Write('[')
    $first = $true
    foreach ($row in $table.Rows) {
        if ($first) { $first = $false } else { $w.Write(",`n") }
        $w.Write('{')
        for ($c = 0; $c -lt $columns.Count; $c++) {
            if ($c) { $w.Write(',') }
            $w.Write($keys[$c])
            $w.Write((Format-Value $row[$c]))
        }
        $w.Write('}')
    }
    $w.Write(']')
}

function Write-List($w, $values) {
    $w.Write('[' + ((@($values) | ForEach-Object { Format-Value $_ }) -join ',') + ']')
}

function Read-Query([string]$sql, [hashtable]$parameters = @{}) {
    $command = $Connection.CreateCommand()
    $command.CommandText = $sql
    $command.CommandTimeout = $Timeout
    foreach ($p in $parameters.GetEnumerator()) { [void]$command.Parameters.AddWithValue($p.Key, $p.Value) }
    $table = New-Object System.Data.DataTable
    [void](New-Object System.Data.SqlClient.SqlDataAdapter $command).Fill($table)
    return , $table   # the comma stops PowerShell from unrolling it into rows
}

function Quote-Name([string]$name) { return '[' + $name.Replace(']', ']]') + ']' }

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if ($MyInvocation.InvocationName -eq '.') { return }   # dot-sourced: functions only

$connectionString = "Server=$Server;Database=master;Application Name=SQL Map (read only);ApplicationIntent=ReadOnly;Connect Timeout=30"
if ($User) {
    $password = Read-Host "Password for $User" -AsSecureString
    $password.MakeReadOnly()
    $credential = New-Object System.Data.SqlClient.SqlCredential($User, $password)
    $Connection = New-Object System.Data.SqlClient.SqlConnection($connectionString, $credential)
} else {
    $Connection = New-Object System.Data.SqlClient.SqlConnection("$connectionString;Integrated Security=SSPI")
}

Write-Host "Connecting to $Server..."
try { $Connection.Open() } catch { Write-Host "Could not connect: $($_.Exception.Message)" -ForegroundColor Red; exit 1 }

$start = Get-Date
$serverName = (Read-Query 'SELECT @@SERVERNAME AS s').Rows[0].s
if ($serverName -is [System.DBNull] -or -not $serverName) { $serverName = $Server }

# Check the databases before writing anything.
$realDatabases = @()
foreach ($requested in $Databases) {
    $r = Read-Query 'SELECT name, HAS_DBACCESS(name) AS access FROM sys.databases WHERE name = @n' @{ '@n' = $requested }
    if ($r.Rows.Count -eq 0) { Write-Host "Database ""$requested"" does not exist on $serverName." -ForegroundColor Red; exit 1 }
    if ($r.Rows[0].access -ne 1) { Write-Host "Your login has no access to database ""$requested""." -ForegroundColor Red; exit 1 }
    $realDatabases += [string]$r.Rows[0].name
}

if (-not $Output) {
    $Output = ("extraction_{0}_{1}_{2:yyyyMMdd}.json" -f $serverName, ($realDatabases -join '_'), (Get-Date)) -replace '[^\w.-]+', '-'
}
$path = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
$warnings = New-Object System.Collections.Generic.List[string]
$w = New-Object System.IO.StreamWriter($path, $false, (New-Object System.Text.UTF8Encoding $false))
$ok = $false
try {
    $w.Write('{"servidor":' + (Escape-Json $serverName))
    $w.Write(',"fecha":' + (Escape-Json (Get-Date -Format 'yyyy-MM-dd HH:mm')))
    $w.Write(',"sistema":')
    Write-List $w ((Read-Query $QuerySystem).Rows | ForEach-Object { $_.name })

    $w.Write(',"bases":{')
    $firstDatabase = $true
    foreach ($database in $realDatabases) {
        if ($firstDatabase) { $firstDatabase = $false } else { $w.Write(',') }
        $w.Write((Escape-Json $database) + ':{')
        Write-Host "`n$database"
        $firstQuery = $true
        foreach ($key in $Queries.Keys) {
            $t0 = Get-Date
            try {
                $table = Read-Query ($Queries[$key].Replace('{bd}', (Quote-Name $database)))
            } catch {
                if ($key -in $Optional) {
                    $warnings.Add("${database}: could not read $key ($($_.Exception.Message))")
                    $table = New-Object System.Data.DataTable
                } else { throw }
            }
            if ($firstQuery) { $firstQuery = $false } else { $w.Write(',') }
            $w.Write('"' + $key + '":')
            Write-Table $w $table
            Write-Host ("  {0,-13} {1,7}  ({2:N1} s)" -f $key, $table.Rows.Count, ((Get-Date) - $t0).TotalSeconds)
            if ($key -eq 'modulos') {
                $noCode = @($table.Select('definition IS NULL')).Count
                if ($noCode) {
                    $warnings.Add("${database}: $noCode modules without visible code (encrypted or no VIEW DEFINITION permission): their writes and dynamic SQL are not analysed.")
                }
            }
        }
        $w.Write('}')
    }
    $w.Write('}')

    $w.Write(',"jobs":')
    if ($NoJobs) {
        $w.Write('[]')
    } else {
        try {
            $jobs = Read-Query $QueryJobs
            Write-Table $w $jobs
            Write-Host "`nSQL Agent jobs: $($jobs.Rows.Count) steps"
        } catch {
            $w.Write('[]')
            $warnings.Add("Could not read the jobs in msdb ($($_.Exception.Message)). The SQLAgentReaderRole role (or similar) is needed.")
            Write-Host "`nSQL Agent jobs: no permission on msdb, skipped"
        }
    }
    $w.Write(',"avisos":')
    Write-List $w $warnings
    $w.Write('}')
    $ok = $true
} catch {
    Write-Host "`nError: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    $w.Close()
    $Connection.Close()
    if (-not $ok) { Remove-Item $path -ErrorAction SilentlyContinue; exit 1 }
}

foreach ($a in $warnings) { Write-Host "WARNING: $a" -ForegroundColor Yellow }
$mb = (Get-Item $path).Length / 1MB
Write-Host ("`nDone in {0:N0} s: {1} ({2:N1} MB)" -f ((Get-Date) - $start).TotalSeconds, $path, $mb) -ForegroundColor Green
Write-Host "Copy it to a machine with Python and run:  python extraer.py --from $(Split-Path $path -Leaf)"
