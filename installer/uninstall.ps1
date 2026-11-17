<#
  Removes the Windows services. The database, backups, snapshots and keys in
  ProgramData\Nil Psikoloji\Klinik are KEPT: reinstalling picks them up again.
#>
param([Parameter(Mandatory = $true)][string]$App)

$ErrorActionPreference = 'Continue'
foreach ($id in @('KlinikApp', 'KlinikJobs', 'KlinikKasa', 'KlinikSync')) {
    $exe = Join-Path $App "services\$id.exe"
    if (Get-Service -Name $id -ErrorAction SilentlyContinue) {
        Stop-Service -Name $id -Force -ErrorAction SilentlyContinue
        if (Test-Path $exe) { & $exe uninstall | Out-Null } else { sc.exe delete $id | Out-Null }
    }
}
if (Get-Service -Name 'KlinikPostgres' -ErrorAction SilentlyContinue) {
    Stop-Service -Name 'KlinikPostgres' -Force -ErrorAction SilentlyContinue
    & (Join-Path $App 'pgsql\bin\pg_ctl.exe') unregister -N KlinikPostgres | Out-Null
}
Get-NetFirewallRule -DisplayName 'Nil Psikoloji Klinik (HTTPS)' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
