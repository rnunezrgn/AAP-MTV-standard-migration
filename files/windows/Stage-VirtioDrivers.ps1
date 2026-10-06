# Stages the latest virtio-win drivers into the Windows driver store BEFORE
# migration, without installing services. When the VirtIO hardware appears on
# OpenShift Virtualization, Plug and Play picks the newest staged version.
param(
    [Parameter(Mandatory)][string]$IsoPath,
    [string[]]$Drivers = @('viostor', 'vioscsi', 'NetKVM', 'Balloon', 'vioserial', 'viorng')
)
$ErrorActionPreference = 'Stop'
$os = Get-CimInstance Win32_OperatingSystem
$build = [int]$os.BuildNumber
$osDir = if ($os.ProductType -ne 1) {
    if ($build -ge 26100) { '2k25' } elseif ($build -ge 20348) { '2k22' } elseif ($build -ge 17763) { '2k19' }
    elseif ($build -ge 14393) { '2k16' } else { throw "Unsupported Windows Server build $build" }
} else {
    if ($build -ge 22000) { 'w11' } elseif ($build -ge 10240) { 'w10' } else { throw "Unsupported Windows client build $build" }
}

$img = Mount-DiskImage -ImagePath $IsoPath -PassThru
try {
    $letter = ($img | Get-Volume).DriveLetter
    $result = foreach ($d in $Drivers) {
        $dir = "$($letter):\$d\$osDir\amd64"
        if (-not (Test-Path $dir)) {
            [ordered]@{ driver = $d; staged = $false; infs = @(); detail = "not on ISO: $d\$osDir\amd64" }
            continue
        }
        $infs = foreach ($inf in Get-ChildItem -Path $dir -Filter *.inf) {
            $m = Select-String -Path $inf.FullName -Pattern '^\s*DriverVer\s*=\s*[^,]+,\s*([0-9.]+)' | Select-Object -First 1
            [ordered]@{ inf = $inf.BaseName.ToLower(); version = if ($m) { $m.Matches[0].Groups[1].Value } else { '' } }
        }
        $out = & pnputil.exe /add-driver "$dir\*.inf" 2>&1 | Out-String
        [ordered]@{ driver = $d; staged = ($LASTEXITCODE -in 0, 259, 3010); exit = $LASTEXITCODE; infs = @($infs); detail = $out.Trim() }
    }
} finally {
    Dismount-DiskImage -ImagePath $IsoPath | Out-Null
}

$baseline = @{}
foreach ($r in $result) { foreach ($i in $r.infs) { if ($i.version) { $baseline[$i.inf] = $i.version } } }
$Ansible.Result = [ordered]@{ os_dir = $osDir; drivers = @($result); baseline = $baseline }
$Ansible.Changed = $true
