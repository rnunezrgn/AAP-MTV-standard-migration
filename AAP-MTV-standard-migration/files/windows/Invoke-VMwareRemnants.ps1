# Inventory or remove VMware leftovers after virt-v2v has uninstalled VMware Tools.
#   -Mode Inventory : read-only, used before and after migration
#   -Mode Remove    : removes what is left, then re-inventories
# Present (non-ghost) VMware devices are never removed.
param(
    [ValidateSet('Inventory', 'Remove')][string]$Mode = 'Inventory',
    [string]$Exclude = ''
)
$ErrorActionPreference = 'Continue'
$devPattern  = 'VMware|vmxnet|PVSCSI|VMCI|vSockets|VMware SVGA'
$svcNames    = @('VMTools', 'VGAuthService', 'vmvss', 'vm3dservice', 'VMwareCAFManagementAgentHost', 'VMwareCAFCommAmqpListener')
$fsPaths     = @("$env:ProgramFiles\VMware", "${env:ProgramFiles(x86)}\VMware", "$env:ProgramData\VMware",
                 "$env:ProgramFiles\Common Files\VMware")
$regPaths    = @('HKLM:\SOFTWARE\VMware, Inc.', 'HKLM:\SOFTWARE\WOW6432Node\VMware, Inc.')
$runKey      = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run'
$build       = [int](Get-CimInstance Win32_OperatingSystem).BuildNumber

function Test-Keep([string]$text) { return ($Exclude -and $text -match $Exclude) }

function Get-Remnants {
    $f = [ordered]@{}
    $uninst = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
              'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
    $f.products = @(Get-ItemProperty $uninst -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match 'VMware' -and $_.Publisher -match 'VMware' -and -not (Test-Keep $_.DisplayName) } |
        ForEach-Object { [ordered]@{ name = $_.DisplayName; version = "$($_.DisplayVersion)"; code = $_.PSChildName } })
    $f.services = @(Get-CimInstance Win32_Service |
        Where-Object { ($_.Name -in $svcNames -or $_.PathName -match '\\VMware\\') -and -not (Test-Keep $_.PathName) } |
        ForEach-Object { [ordered]@{ name = $_.Name; state = $_.State; path = $_.PathName } })
    $f.driver_packages = @(Get-WindowsDriver -Online | Where-Object { $_.ProviderName -match 'VMware' } |
        ForEach-Object { [ordered]@{ published = $_.Driver; original = [IO.Path]::GetFileName($_.OriginalFileName); version = "$($_.Version)" } })
    $devs = @(Get-PnpDevice -ErrorAction SilentlyContinue |
        Where-Object { $_.FriendlyName -match $devPattern -or $_.Manufacturer -match 'VMware' })
    $f.ghost_devices = @($devs | Where-Object { -not $_.Present } |
        ForEach-Object { [ordered]@{ name = $_.FriendlyName; id = $_.InstanceId; class = $_.Class } })
    $f.present_devices = @($devs | Where-Object { $_.Present } |
        ForEach-Object { [ordered]@{ name = $_.FriendlyName; id = $_.InstanceId } })
    $f.paths = @($fsPaths | Where-Object { $_ -and (Test-Path $_) })
    $f.registry = @($regPaths | Where-Object { Test-Path $_ })
    $run = Get-ItemProperty $runKey -ErrorAction SilentlyContinue
    $f.run_values = @(if ($run) { $run.PSObject.Properties | Where-Object { "$($_.Value)" -match 'VMware' } | ForEach-Object { $_.Name } })
    $f.count = ($f.products.Count + $f.services.Count + $f.driver_packages.Count + $f.ghost_devices.Count +
                $f.paths.Count + $f.registry.Count + $f.run_values.Count)
    return $f
}

$before  = Get-Remnants
$removed = [System.Collections.Generic.List[string]]::new()
$errors  = [System.Collections.Generic.List[string]]::new()
$manual  = [System.Collections.Generic.List[string]]::new()

if ($Mode -eq 'Remove') {
    foreach ($p in $before.products) {
        if ($p.code -match '^\{[0-9A-Fa-f-]{36}\}$') {
            $proc = Start-Process msiexec.exe -ArgumentList "/x $($p.code) /qn /norestart" -Wait -PassThru
            if ($proc.ExitCode -in 0, 1605, 1614, 3010) { $removed.Add("product: $($p.name)") }
            else { $errors.Add("product $($p.name): msiexec exit $($proc.ExitCode)") }
        } else { $manual.Add("product $($p.name): not an MSI, remove manually") }
    }
    foreach ($s in $before.services) {
        Stop-Service -Name $s.name -Force -ErrorAction SilentlyContinue
        $out = & sc.exe delete $s.name 2>&1
        if ($LASTEXITCODE -in 0, 1060) { $removed.Add("service: $($s.name)") } else { $errors.Add("service $($s.name): $out") }
    }
    foreach ($d in $before.driver_packages) {
        $out = & pnputil.exe /delete-driver $d.published /uninstall /force 2>&1
        if ($LASTEXITCODE -eq 0) { $removed.Add("driver: $($d.original) ($($d.published))") }
        else { $errors.Add("driver $($d.published): $($out | Out-String)") }
    }
    foreach ($g in $before.ghost_devices) {
        if ($build -ge 19041) {
            $out = & pnputil.exe /remove-device "$($g.id)" 2>&1
            if ($LASTEXITCODE -eq 0) { $removed.Add("ghost device: $($g.name)") }
            else { $errors.Add("ghost $($g.name): $($out | Out-String)") }
        } else { $manual.Add("ghost $($g.name): build $build has no pnputil /remove-device; use devcon remove") }
    }
    foreach ($p in $before.paths)    { try { Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction Stop; $removed.Add("path: $p") } catch { $errors.Add("path ${p}: $_") } }
    foreach ($k in $before.registry) { try { Remove-Item -Path $k -Recurse -Force -ErrorAction Stop; $removed.Add("registry: $k") } catch { $errors.Add("registry ${k}: $_") } }
    foreach ($v in $before.run_values) { Remove-ItemProperty -Path $runKey -Name $v -ErrorAction SilentlyContinue; $removed.Add("run value: $v") }
}

$after = if ($Mode -eq 'Remove') { Get-Remnants } else { $before }
$Ansible.Result = [ordered]@{
    mode            = $Mode
    found           = $before
    removed         = @($removed)
    errors          = @($errors)
    manual          = @($manual)
    remaining       = $after
    remaining_count = $after.count
    reboot_required = ($removed.Count -gt 0)
}
$Ansible.Changed = ($removed.Count -gt 0)
