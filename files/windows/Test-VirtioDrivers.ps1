# Proves which VirtIO drivers are bound to devices after migration and compares
# them with the baseline staged before migration. With -IsoPath it installs the
# newer drivers from the ISO and checks again.
param(
    [string]$BaselineJson = '{}',
    [string]$IsoPath = '',
    [string]$OsDir = ''
)
$ErrorActionPreference = 'Stop'
$Baseline = @{}
($BaselineJson | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $Baseline[$_.Name.ToLower()] = "$($_.Value)" }

function Get-Bound {
    $store = @{}
    Get-WindowsDriver -Online | Where-Object { $_.ProviderName -match 'Red Hat' } | ForEach-Object {
        $store[$_.Driver.ToLower()] = [IO.Path]::GetFileNameWithoutExtension($_.OriginalFileName).ToLower()
    }
    @(Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.InfName -and $store.ContainsKey($_.InfName.ToLower()) } |
        ForEach-Object { [ordered]@{ inf = $store[$_.InfName.ToLower()]; device = $_.DeviceName; version = "$($_.DriverVersion)" } })
}

function Compare-Bound($bound) {
    foreach ($b in $bound) {
        $want = $Baseline[$b.inf]
        $ok = (-not $want) -or ([version]$b.version -ge [version]$want)
        [ordered]@{ inf = $b.inf; device = $b.device; version = $b.version; baseline = "$want"; ok = $ok }
    }
}

$bound = Get-Bound
$cmp = @(Compare-Bound $bound)
$updated = $false
if ($IsoPath -and ($cmp | Where-Object { -not $_.ok })) {
    $img = Mount-DiskImage -ImagePath $IsoPath -PassThru
    try {
        $letter = ($img | Get-Volume).DriveLetter
        foreach ($c in $cmp | Where-Object { -not $_.ok }) {
            Get-ChildItem "$($letter):\" -Recurse -Filter "$($c.inf).inf" |
                Where-Object { $_.FullName -match "\\$OsDir\\amd64\\" } |
                ForEach-Object { & pnputil.exe /add-driver $_.FullName /install | Out-Null }
        }
        & pnputil.exe /scan-devices | Out-Null
        $updated = $true
    } finally { Dismount-DiskImage -ImagePath $IsoPath | Out-Null }
    $cmp = @(Compare-Bound (Get-Bound))
}

$infs = @($cmp | ForEach-Object { $_.inf })
$Ansible.Result = [ordered]@{
    bound      = $cmp
    storage_ok = [bool]($infs -contains 'viostor' -or $infs -contains 'vioscsi')
    network_ok = [bool]($infs -contains 'netkvm')
    all_ok     = -not ($cmp | Where-Object { -not $_.ok })
    updated    = $updated
}
$Ansible.Changed = $updated
