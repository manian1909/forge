# Switch an already open YouTube / YouTube Music tab in Chrome to a new address, so a second song does not open a second tab.
# Input (environment): JARVIS_URL = the address, JARVIS_SERVICE = youtube_music or youtube.
# Output: "REUSED: <tab title>" or "NONE" (no such tab, so the caller opens a new one) or "NOBAR".
# Uses Windows UI Automation: no admin rights, no browser extension, no debugging port.
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms
Add-Type @"
using System; using System.Runtime.InteropServices;
public class Fg { [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
                  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
                  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h); }
"@
$AE = [System.Windows.Automation.AutomationElement]
$url = $env:JARVIS_URL
$music = $env:JARVIS_SERVICE -ne 'youtube'
$root = $AE::RootElement
$wins = $root.FindAll('Children', (New-Object System.Windows.Automation.PropertyCondition($AE::ClassNameProperty, 'Chrome_WidgetWin_1')))
$best = $null
foreach ($w in $wins) {
    $tabs = $w.FindAll('Descendants', (New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::TabItem)))
    foreach ($t in $tabs) {
        $name = $t.Current.Name
        $title = $name -replace ' - (Audio playing|Memory usage|High memory usage).*$', ''
        $isMusic = $title -like '*YouTube Music*'
        $isVideo = ($title -like '*- YouTube') -and -not $isMusic
        if (($music -and $isMusic) -or (-not $music -and $isVideo)) {
            $playing = $name -like '*Audio playing*'
            if ($null -eq $best -or ($playing -and -not $best.playing)) {
                $best = @{ win = $w; tab = $t; title = $title; playing = $playing }
            }
        }
    }
}
if ($null -eq $best) { 'NONE'; exit }
$w = $best.win
$h = [IntPtr]$w.Current.NativeWindowHandle
if ([Fg]::IsIconic($h)) { [Fg]::ShowWindow($h, 9) | Out-Null }
[Fg]::SetForegroundWindow($h) | Out-Null
$best.tab.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
Start-Sleep -Milliseconds 150
$edit = $w.FindFirst('Descendants', (New-Object System.Windows.Automation.AndCondition(
    (New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::Edit)),
    (New-Object System.Windows.Automation.PropertyCondition($AE::NameProperty, 'Address and search bar')))))
if (-not $edit) { 'NOBAR'; exit }
$edit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue($url)
$edit.SetFocus()
Start-Sleep -Milliseconds 100
[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
"REUSED: $($best.title)"
