# Makes sure every BitLocker volume has a recovery password and escrows it.
# The recovery password itself is never returned to Ansible.
param([ValidateSet('ad', 'aad', 'none')][string]$Method = 'ad')
$ErrorActionPreference = 'Stop'
$out = @()
foreach ($v in Get-BitLockerVolume | Where-Object { $_.VolumeStatus -ne 'FullyDecrypted' }) {
    $added = $false
    $rp = @($v.KeyProtector | Where-Object KeyProtectorType -eq 'RecoveryPassword')
    if (-not $rp) {
        Add-BitLockerKeyProtector -MountPoint $v.MountPoint -RecoveryPasswordProtector -WarningAction SilentlyContinue | Out-Null
        $rp = @((Get-BitLockerVolume -MountPoint $v.MountPoint).KeyProtector | Where-Object KeyProtectorType -eq 'RecoveryPassword')
        $added = $true
    }
    $escrowed = $false; $err = ''
    foreach ($p in $rp) {
        try {
            switch ($Method) {
                'ad'  { Backup-BitLockerKeyProtector -MountPoint $v.MountPoint -KeyProtectorId $p.KeyProtectorId | Out-Null; $escrowed = $true }
                'aad' { BackupToAAD-BitLockerKeyProtector -MountPoint $v.MountPoint -KeyProtectorId $p.KeyProtectorId | Out-Null; $escrowed = $true }
            }
        } catch { $err = "$_" }
    }
    $out += [ordered]@{ mount = $v.MountPoint; protectors = $rp.Count; added_recovery_password = $added; escrowed = $escrowed; error = $err }
}
$Ansible.Result = @($out)
$Ansible.Changed = [bool]($out | Where-Object { $_.added_recovery_password -or $_.escrowed })
