param(
    [ValidateSet('Install','Remove','Status')][string]$Action = 'Status',
    [ValidateSet('User','Machine')][string]$Scope = 'User',
    [int]$Workers = 2
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$PythonPath = (Resolve-Path -LiteralPath (Join-Path $ProjectRoot '.venv/Scripts/python.exe')).Path
$TaskName = 'TaxAgent-Class2-Worker'
if ($Scope -eq 'Machine') {
    if ($Action -eq 'Install') {
        $arguments = '-m tax_agent.runtime.service --workers ' + $Workers
        $taskAction = New-ScheduledTaskAction -Execute $PythonPath -Argument $arguments -WorkingDirectory $ProjectRoot
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    } elseif ($Action -eq 'Remove') {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
    exit
}
$StartupDir = [Environment]::GetFolderPath('Startup')
$LauncherPath = Join-Path $StartupDir ($TaskName + '.vbs')
if ($Action -eq 'Install') {
    $command = '"' + $PythonPath + '" -m tax_agent.runtime.service --workers ' + $Workers
    $escapedCommand = $command.Replace('"','""')
    $escapedRoot = $ProjectRoot.Replace('"','""')
    $content = "Set shell = CreateObject(""WScript.Shell"")" + [Environment]::NewLine +
               'shell.CurrentDirectory = "' + $escapedRoot + '"' + [Environment]::NewLine +
               'shell.Run "' + $escapedCommand + '", 0, False'
    Set-Content -LiteralPath $LauncherPath -Value $content -Encoding Unicode
} elseif ($Action -eq 'Remove') {
    Remove-Item -LiteralPath $LauncherPath
}
[PSCustomObject]@{Scope='User'; Trigger='At user login'; Installed=(Test-Path -LiteralPath $LauncherPath); Path=$LauncherPath}
