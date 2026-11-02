# 只读检查：锁屏 / 屏保 / 睡眠 / 唤醒密码 / 自动登录 / orc-sjz 自启
# 不会修改任何系统设置

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$warn = 0
function Report($level, $name, $detail) {
    $color = switch ($level) { "PASS" { "Green" } "WARN" { "Yellow" } default { "Gray" } }
    Write-Host ("[{0}] {1}: {2}" -f $level, $name, $detail) -ForegroundColor $color
    if ($level -eq "WARN") { $script:warn++ }
}

function Get-PowerIndexes($settingGuid) {
    $out = powercfg /QUERY SCHEME_CURRENT $settingGuid 2>$null
    $matches = [regex]::Matches(($out -join "`n"), "0x[0-9a-fA-F]{8}")
    if ($matches.Count -ge 2) {
        return @(
            [Convert]::ToInt64($matches[$matches.Count - 2].Value, 16),
            [Convert]::ToInt64($matches[$matches.Count - 1].Value, 16)
        )
    }
    return $null
}

Write-Host "=== 无人值守环境检查（只读） ===" -ForegroundColor Cyan

# 1. 屏保
$desk = Get-ItemProperty "HKCU:\Control Panel\Desktop" -ErrorAction SilentlyContinue
$active = "$($desk.ScreenSaveActive)"
$secure = "$($desk.ScreenSaverIsSecure)"
$timeout = $desk.ScreenSaveTimeOut
$scr = $desk.'SCRNSAVE.EXE'
if ([string]::IsNullOrWhiteSpace($scr)) {
    Report "PASS" "屏保" ("未设置屏保程序（ScreenSaveActive={0}）" -f $active)
} elseif ($active -eq "1") {
    Report "WARN" "屏保" ("已启用: {0}，等待 {1} 秒" -f $scr, $timeout)
} else {
    Report "PASS" "屏保" "未启用"
}
if ($secure -eq "1") {
    Report "WARN" "恢复时显示登录屏幕" "已勾选（无密码也会锁屏，必须取消）"
} else {
    Report "PASS" "恢复时显示登录屏幕" "未勾选"
}

# 2. 自动锁屏（组策略：计算机不活动限制）
$sys = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" -ErrorAction SilentlyContinue
$inactivity = $sys.InactivityTimeoutSecs
if ($null -ne $inactivity -and [int]$inactivity -gt 0) {
    Report "WARN" "不活动自动锁屏" ("{0} 秒后锁屏" -f $inactivity)
} else {
    Report "PASS" "不活动自动锁屏" "未启用"
}

# 3. 唤醒后是否需要密码（电源策略 CONSOLELOCK）
$subNone = (powercfg /QUERY SCHEME_CURRENT SUB_NONE) -join "`n"
if ($subNone -match "0e796bdb-100d-47d6-a2d5-f7d2daa51f51") {
    $lock = Get-PowerIndexes "0e796bdb-100d-47d6-a2d5-f7d2daa51f51"
    if ($lock -and $lock[0] -eq 1) {
        Report "WARN" "唤醒后需要密码" "已开启（交流电）；睡眠唤醒会停在锁屏"
    } elseif ($lock) {
        Report "PASS" "唤醒后需要密码" "未开启（交流电）"
    } else {
        Report "INFO" "唤醒后需要密码" "设置存在但无法读取数值"
    }
} else {
    Report "INFO" "唤醒后需要密码" "当前系统未暴露此电源设置（可能由策略/现代待机管理）"
}

# 4. 睡眠超时（STANDBYIDLE）
$sleepOut = (powercfg /QUERY SCHEME_CURRENT SUB_SLEEP STANDBYIDLE) -join "`n"
$sleepMatches = [regex]::Matches($sleepOut, "0x[0-9a-fA-F]{8}")
$sleep = $null
if ($sleepMatches.Count -ge 2) {
    $sleep = @(
        [Convert]::ToInt64($sleepMatches[$sleepMatches.Count - 2].Value, 16),
        [Convert]::ToInt64($sleepMatches[$sleepMatches.Count - 1].Value, 16)
    )
}
if ($sleep) {
    $acText = if ($sleep[0] -eq 0) { "从不" } else { "$($sleep[0]) 秒" }
    $dcText = if ($sleep[1] -eq 0) { "从不" } else { "$($sleep[1]) 秒" }
    if ($sleep[0] -gt 0) {
        Report "WARN" "睡眠超时" ("交流电 {0} / 电池 {1}（程序运行时已阻止睡眠，未运行时会睡）" -f $acText, $dcText)
    } else {
        Report "PASS" "睡眠超时" ("交流电 {0} / 电池 {1}" -f $acText, $dcText)
    }
} else {
    Report "INFO" "睡眠超时" "无法读取"
}

# 5. 动态锁
$winlogonUser = Get-ItemProperty "HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Winlogon" -ErrorAction SilentlyContinue
$goodbye = "$($winlogonUser.EnableGoodbye)"
if ($goodbye -eq "1") {
    Report "WARN" "动态锁" "已启用（离开后会自动锁屏）"
} else {
    Report "PASS" "动态锁" "未启用"
}

# 6. 自动登录
$winlogon = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -ErrorAction SilentlyContinue
$auto = "$($winlogon.AutoAdminLogon)"
if ($auto -eq "1") {
    Report "PASS" "自动登录" ("已配置，用户: {0}" -f $winlogon.DefaultUserName)
} else {
    Report "INFO" "自动登录" "未配置（关机后需要人工登录）"
}

# 7. 当前会话是否锁屏
try {
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public class DeskCheck {
    [DllImport("user32.dll", SetLastError=true)]
    public static extern IntPtr OpenInputDesktop(int dwFlags, bool fInherit, int dwDesiredAccess);
    [DllImport("user32.dll")]
    public static extern bool CloseDesktop(IntPtr hDesktop);
    public static bool IsLocked() {
        IntPtr h = OpenInputDesktop(0, false, 0x0100);
        if (h == IntPtr.Zero) return true;
        CloseDesktop(h);
        return false;
    }
}
"@
    if ([DeskCheck]::IsLocked()) {
        Report "WARN" "当前桌面" "锁屏/安全桌面（程序会跳过并重试）"
    } else {
        Report "PASS" "当前桌面" "已登录且未锁屏"
    }
} catch {
    Report "INFO" "当前桌面" "检测失败: $($_.Exception.Message)"
}

# 8. 自启项（计划任务 / 启动文件夹 / Run 键）
$taskHits = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
    ($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -match "orc-sjz"
}
if ($taskHits) {
    foreach ($t in $taskHits) {
        Report "PASS" "计划任务" ("{0}{1}（{2}）" -f $t.TaskPath, $t.TaskName, $t.State)
    }
} else {
    Report "INFO" "计划任务" "未发现 orc-sjz"
}
$startup = [Environment]::GetFolderPath("Startup")
$shortcut = Get-ChildItem $startup -ErrorAction SilentlyContinue | Where-Object { $_.Name -match "orc-sjz" }
if ($shortcut) {
    Report "INFO" "启动文件夹" (($shortcut | Select-Object -ExpandProperty Name) -join ", ")
} else {
    Report "INFO" "启动文件夹" "未发现 orc-sjz"
}
$run = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -ErrorAction SilentlyContinue
$runHit = $run.PSObject.Properties | Where-Object { "$($_.Value)" -match "orc-sjz" }
if ($runHit) {
    Report "INFO" "注册表 Run" (($runHit | ForEach-Object { $_.Name }) -join ", ")
} else {
    Report "INFO" "注册表 Run" "未发现 orc-sjz"
}

# 9. 电源与唤醒计时器提示
$activeScheme = (powercfg /GETACTIVESCHEME) -join " "
Report "INFO" "电源计划" $activeScheme
Report "INFO" "唤醒计时器" "如需睡眠后自动唤醒，请在计划任务勾选'唤醒计算机运行此任务'并在电源选项允许唤醒计时器"

Write-Host ""
if ($warn -eq 0) {
    Write-Host "结论：未发现会影响无人值守的锁屏/屏保设置" -ForegroundColor Green
} else {
    Write-Host ("结论：发现 {0} 项需要关注，请按上面 WARN 处理" -f $warn) -ForegroundColor Yellow
}
Write-Host "（本脚本只读，不会修改任何设置）" -ForegroundColor Gray
