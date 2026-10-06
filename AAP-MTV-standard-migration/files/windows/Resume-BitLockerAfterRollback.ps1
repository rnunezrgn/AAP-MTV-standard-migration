# Rollback: the VM is back on VMware with its original vTPM, so resuming is enough.
$ErrorActionPreference = 'Stop'
$out = @()
foreach ($v in Get-BitLockerVolume | Where-Object { $_.ProtectionStatus -eq 'Off' -and $_.VolumeStatus -ne 'FullyDecrypted' }) {
    Resume-BitLocker -MountPoint $v.MountPoint | Out-Null
    $out += $v.MountPoint
}
$Ansible.Result = @($out)
$Ansible.Changed = ($out.Count -gt 0)
