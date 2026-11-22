# Windows toast for the Notification and Stop hooks (slice T-1, D-111), built only from what Windows
# PowerShell 5.1 has: the WinRT ToastNotificationManager and PowerShell's own AppUserModelID, no modules.
# run.sh starts it from .claude/settings.local.json. The title names the project; the text is the
# hook's message (Notification) or "Turn finished" (Stop).
$ErrorActionPreference = 'Stop'
$text = 'Turn finished'
try {
    $hook = [Console]::In.ReadToEnd() | ConvertFrom-Json
    if ($hook.message) { $text = [string]$hook.message }
} catch { }
if ($env:CLAUDE_PROJECT_DIR) { $title = Split-Path -Leaf $env:CLAUDE_PROJECT_DIR } else { $title = 'Claude Code' }
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$lines = $xml.GetElementsByTagName('text')
[void]$lines.Item(0).AppendChild($xml.CreateTextNode($title))
[void]$lines.Item(1).AppendChild($xml.CreateTextNode($text))
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$powershellId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($powershellId).Show($toast)
