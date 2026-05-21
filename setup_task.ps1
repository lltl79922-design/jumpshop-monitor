# 创建 Windows 计划任务 - 每5分钟运行一次监控
$action = New-ScheduledTaskAction -Execute "python" -Argument "monitor.py" -WorkingDirectory "C:\Users\刘天龙\Desktop\jumpshop自动监控"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration ([TimeSpan]::MaxValue)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "JumpShopMonitor" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Jump Shop 商品监控 - 每5分钟检测上新和补货"

Write-Output "Task 'JumpShopMonitor' registered. Check Task Scheduler to confirm."
Write-Output "To remove: Unregister-ScheduledTask -TaskName 'JumpShopMonitor' -Confirm:$false"
