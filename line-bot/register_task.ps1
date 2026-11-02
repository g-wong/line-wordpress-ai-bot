# poller.py を1分ごとに実行するタスクを登録する（ログオン中のみ実行）
# 実行: powershell -ExecutionPolicy Bypass -File register_task.ps1
$py = (Get-Command pythonw -ErrorAction Stop).Source
$script = Join-Path $PSScriptRoot 'poller.py'

$action = New-ScheduledTaskAction -Execute $py -Argument "`"$script`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName 'LINE-HomepageBot' -Action $action -Trigger $trigger -Settings $settings `
    -Description 'LINE からのホームページ更新指示を処理する' -Force | Out-Null
Write-Host '登録しました: LINE-HomepageBot（停止するには Unregister-ScheduledTask LINE-HomepageBot）'
