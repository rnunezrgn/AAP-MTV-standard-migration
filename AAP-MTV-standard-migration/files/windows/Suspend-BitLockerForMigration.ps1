# Suspends BitLocker on every protected volume until it is resumed explicitly
# (RebootCount 0), so the conversion reboots and the first boot on OpenShift
# Virtualization do not stop at the recovery screen.
$ErrorActionPreference = 'Stop'
$out = @()
foreach ($v in Get-BitLockerVolume | Where-Object { $_.ProtectionStatus -eq 'On' }) {
    Suspend-BitLocker -MountPoint $v.MountPoint -RebootCount 0 | Out-Null
    $now = Get-BitLockerVolume -MountPoint $v.MountPoint
    $out += [ordered]@{ mount = $v.MountPoint; protection = "$($now.ProtectionStatus)" }
}
$Ansible.Result = @($out)
$Ansible.Changed = ($out.Count -gt 0)
