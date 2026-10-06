# After migration: bind BitLocker to the new vTPM, resume protection, and
# rotate the recovery password. TPM+PIN and startup-key protectors are left
# alone and reported, because they need a person.
param(
    [ValidateSet('ad', 'aad', 'none')][string]$Method = 'ad',
    [bool]$Rotate = $true
)
$ErrorActionPreference = 'Stop'
$log = [System.Collections.Generic.List[string]]::new()
$manual = [System.Collections.Generic.List[string]]::new()

$tpm = Get-Tpm
if (-not $tpm.TpmPresent) { throw 'No TPM is visible in the guest. Add a persistent vTPM to the VM first.' }
if (-not $tpm.TpmReady) {
    Initialize-Tpm -AllowClear:$false -AllowPhysicalPresence:$false | Out-Null
    $tpm = Get-Tpm
    $log.Add("TPM initialised, ready=$($tpm.TpmReady)")
}

$sys = $env:SystemDrive
$osv = Get-BitLockerVolume -MountPoint $sys
if ($osv.VolumeStatus -ne 'FullyDecrypted') {
    foreach ($p in $osv.KeyProtector | Where-Object { "$($_.KeyProtectorType)" -match '^Tpm(Pin|StartupKey|PinStartupKey)$' }) {
        $manual.Add("$sys has a $($p.KeyProtectorType) protector: re-create it with the user present")
    }
    foreach ($p in $osv.KeyProtector | Where-Object { "$($_.KeyProtectorType)" -eq 'Tpm' }) {
        Remove-BitLockerKeyProtector -MountPoint $sys -KeyProtectorId $p.KeyProtectorId | Out-Null
        $log.Add("removed old TPM protector $($p.KeyProtectorId)")
    }
    if (-not $manual.Count) {
        Add-BitLockerKeyProtector -MountPoint $sys -TpmProtector -WarningAction SilentlyContinue | Out-Null
        $log.Add('added TPM protector bound to the new vTPM')
    }

    if ($Rotate -and $Method -ne 'none') {
        foreach ($v in Get-BitLockerVolume | Where-Object { $_.VolumeStatus -ne 'FullyDecrypted' }) {
            $old = @($v.KeyProtector | Where-Object KeyProtectorType -eq 'RecoveryPassword' | ForEach-Object KeyProtectorId)
            Add-BitLockerKeyProtector -MountPoint $v.MountPoint -RecoveryPasswordProtector -WarningAction SilentlyContinue | Out-Null
            $new = @((Get-BitLockerVolume -MountPoint $v.MountPoint).KeyProtector |
                Where-Object { $_.KeyProtectorType -eq 'RecoveryPassword' -and $_.KeyProtectorId -notin $old })
            foreach ($n in $new) {
                if ($Method -eq 'ad') { Backup-BitLockerKeyProtector -MountPoint $v.MountPoint -KeyProtectorId $n.KeyProtectorId | Out-Null }
                else { BackupToAAD-BitLockerKeyProtector -MountPoint $v.MountPoint -KeyProtectorId $n.KeyProtectorId | Out-Null }
            }
            foreach ($o in $old) { Remove-BitLockerKeyProtector -MountPoint $v.MountPoint -KeyProtectorId $o | Out-Null }
            $log.Add("$($v.MountPoint): recovery password rotated and escrowed")
        }
    }

    foreach ($v in Get-BitLockerVolume | Where-Object { $_.ProtectionStatus -eq 'Off' -and $_.VolumeStatus -ne 'FullyDecrypted' }) {
        if ($v.MountPoint -eq $sys -and $manual.Count) { continue }
        Resume-BitLocker -MountPoint $v.MountPoint | Out-Null
        $log.Add("$($v.MountPoint): protection resumed")
    }
}

$final = @(Get-BitLockerVolume | ForEach-Object {
    [ordered]@{ mount = $_.MountPoint; protection = "$($_.ProtectionStatus)";
                protectors = @($_.KeyProtector | ForEach-Object { "$($_.KeyProtectorType)" }) } })
$Ansible.Result = [ordered]@{ log = @($log); manual = @($manual); volumes = $final; tpm_ready = [bool]$tpm.TpmReady }
$Ansible.Changed = ($log.Count -gt 0)
