# Collects the guest state AAP compares before and after migration.
# Read-only. Returns one object through $Ansible.Result.
$ErrorActionPreference = 'Stop'
$r = [ordered]@{}

$os = Get-CimInstance Win32_OperatingSystem
$r.os = [ordered]@{
    caption      = $os.Caption
    version      = $os.Version
    build        = [int]$os.BuildNumber
    product_type = [int]$os.ProductType      # 1 client, 2 DC, 3 server
    arch         = $os.OSArchitecture
}
$r.firmware = "$env:firmware_type"           # UEFI or Legacy
try { $r.secure_boot = [bool](Confirm-SecureBootUEFI) } catch { $r.secure_boot = $false }

try {
    $t = Get-Tpm
    $r.tpm = [ordered]@{ present = [bool]$t.TpmPresent; ready = [bool]$t.TpmReady }
} catch { $r.tpm = [ordered]@{ present = $false; ready = $false } }

$r.bitlocker = @()
if (Get-Command Get-BitLockerVolume -ErrorAction SilentlyContinue) {
    $r.bitlocker = @(Get-BitLockerVolume | ForEach-Object {
        [ordered]@{
            mount      = $_.MountPoint
            type       = "$($_.VolumeType)"
            protection = "$($_.ProtectionStatus)"
            status     = "$($_.VolumeStatus)"
            percent    = [double]$_.EncryptionPercentage
            protectors = @($_.KeyProtector | ForEach-Object { "$($_.KeyProtectorType)" })
        }
    })
}

$r.network = @(Get-NetIPConfiguration | Where-Object { $_.NetAdapter.Status -eq 'Up' } | ForEach-Object {
    $if = Get-NetIPInterface -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
    [ordered]@{
        alias       = $_.InterfaceAlias
        description = $_.InterfaceDescription
        mac         = $_.NetAdapter.MacAddress
        ipv4        = @($_.IPv4Address | ForEach-Object { $_.IPAddress })
        prefix      = @($_.IPv4Address | ForEach-Object { [int]$_.PrefixLength })
        gateway     = @($_.IPv4DefaultGateway | ForEach-Object { $_.NextHop })
        dns         = @($_.DNSServer | Where-Object AddressFamily -eq 2 | ForEach-Object { $_.ServerAddresses })
        dhcp        = "$($if.Dhcp)"
    }
})

$cs = Get-CimInstance Win32_ComputerSystem
$r.domain = [ordered]@{ joined = [bool]$cs.PartOfDomain; name = $cs.Domain; secure_channel = $null }
if ($cs.PartOfDomain) {
    try { $r.domain.secure_channel = [bool](Test-ComputerSecureChannel) } catch { $r.domain.secure_channel = $false }
}

$c = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$env:SystemDrive'"
$r.free_gb_system = [math]::Round($c.FreeSpace / 1GB, 1)
$r.pending_reboot = (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') -or
                    (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired')

$r.virtio_store = @(Get-WindowsDriver -Online | Where-Object { $_.ProviderName -match 'Red Hat' } | ForEach-Object {
    [ordered]@{
        inf       = [IO.Path]::GetFileNameWithoutExtension($_.OriginalFileName).ToLower()
        published = $_.Driver
        version   = "$($_.Version)"
    }
})

$ga = Get-Service -Name 'QEMU-GA' -ErrorAction SilentlyContinue
$r.guest_agent = if ($ga) { "$($ga.Status)" } else { 'absent' }
$r.auto_services_stopped = @(Get-CimInstance Win32_Service -Filter "StartMode='Auto' AND State<>'Running'" |
    Where-Object { $_.Name -notmatch '^(sppsvc|RemoteRegistry|gupdate|edgeupdate|MapsBroker|tiledatamodelsvc|wuauserv|BITS)$' } |
    ForEach-Object { $_.Name })

$Ansible.Result = $r
$Ansible.Changed = $false
